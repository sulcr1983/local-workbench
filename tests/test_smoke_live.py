"""真实服务冒烟测试：起一个真的 Waitress，用真 HTTP 打一遍.

为什么要有这一层：现有测试全走 Flask 的 test_client（内存里直接调视图），
绕过了 Waitress、真实 socket、HTTP 头、并发线程。业界标准的冒烟测试是
「打正在跑的实例」（见 smoke-test 惯例：核心路由必须 2xx/3xx，5xx 即失败，
响应体不许泄漏栈）。这里就是那条线。

做法：临时库 + 随机空闲端口 + 子进程启动 app.py，跑完 taskkill /T 收干净。
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROBE_TIMEOUT = 20
_BOOT_TIMEOUT = 45
_state: dict = {"proc": None, "base": "", "db": "", "log": ""}

BAD_MARKERS = ("Traceback (most recent call last)", "Internal Server Error",
               "werkzeug.exceptions", "Method Not Allowed")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(path: str, timeout: int = _PROBE_TIMEOUT):
    """不带代理的 GET：系统代理会把 127.0.0.1 拦成 502."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(_state["base"] + path, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        return err.code, err.read(), err.headers.get("Content-Type", "")


def _alive() -> bool:
    proc = _state["proc"]
    return proc is not None and proc.poll() is None


def _stop():
    proc = _state.get("proc")
    if proc is None:
        return
    if proc.poll() is None:
        # /T 连孙进程一起收；否则 python 子进程会赖着占端口
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    _state["proc"] = None


atexit.register(_stop)


def _server() -> str:
    """起一次、复用；返回 base url."""
    if _state["base"] and _alive():
        return _state["base"]

    db_path = os.path.join(tempfile.mkdtemp(prefix="wb-live-"), "workbench.db")
    port = _free_port()
    log_path = db_path + ".log"
    env = dict(os.environ)
    env.update({
        "WB_DB": db_path,
        "WB_PORT": str(port),
        "WB_NO_SCHED": "1",
        "WB_NO_BROWSER": "1",
        "PYTHONIOENCODING": "utf-8",
    })

    log = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, "-u", "app.py"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    _state.update({"proc": proc, "base": f"http://127.0.0.1:{port}",
                   "db": db_path, "log": log_path})

    deadline = time.time() + _BOOT_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            status, _, _ = _get("/", timeout=3)
            if status in (200, 302):
                return _state["base"]
        except Exception:  # noqa: BLE001 - 还没起来，继续等
            pass
        time.sleep(0.25)

    tail = ""
    try:
        with open(log_path, "rb") as f:
            tail = f.read()[-1500:].decode("utf-8", "replace")
    except OSError:
        pass
    _stop()
    raise AssertionError(f"服务在 {_BOOT_TIMEOUT}s 内没起来，日志尾部：\n{tail}")


def _get_rules() -> list[str]:
    """从 app 对象拿 GET 路由表；只取无参数的（临时库没有数据）."""
    sys.path.insert(0, ROOT)
    from wb import create_app  # noqa: PLC0415 - 必须等环境变量就位
    from wb import db  # noqa: PLC0415

    # 别的测试模块可能已经把库切到别处；这里显式切到本次的临时库，
    # 绝不碰 data/workbench.db。
    db.close_thread_conn()
    db.configure(_state["db"] or os.path.join(
        tempfile.mkdtemp(prefix="wb-rules-"), "x.db"))
    os.environ["WB_NO_SCHED"] = "1"

    rules = []
    for rule in create_app().url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments:
            continue
        if rule.rule.startswith("/static"):
            continue
        rules.append(rule.rule)
    return sorted(set(rules))


# ---------------------------------------------------------------- 用例


def test_live_server_boots_and_serves_home():
    base = _server()
    assert _alive(), "进程已经退出"
    status, body, ctype = _get("/")
    assert status in (200, 302), f"首页 {status}"
    assert "text/html" in ctype, ctype
    assert body, "首页空响应"


def test_live_all_pages_are_not_5xx_and_do_not_leak_traceback():
    _server()
    problems = []
    for path in _get_rules():
        status, body, _ = _get(path)
        if status >= 500:
            problems.append(f"{path} -> {status}")
            continue
        text = body.decode("utf-8", "replace")
        leaked = [m for m in BAD_MARKERS if m in text]
        if leaked:
            problems.append(f"{path} 泄漏 {leaked}")
    assert not problems, "有页面挂了：" + "；".join(problems)


def test_live_static_assets_all_200():
    _server()
    assets = ["app.css", "app.js", "discover.js", "engine.js", "expiries.js",
              "favicon.svg", "logo.svg", "server_detail.js", "settings.js",
              "welcome.js"]
    missing = []
    for name in assets:
        status, body, _ = _get(f"/static/{name}")
        if status != 200 or not body:
            missing.append(f"{name} -> {status}")
    assert not missing, "静态资源缺失：" + "；".join(missing)


def test_live_404_is_chinese_html():
    _server()
    status, body, ctype = _get("/no-such-page-smoke")
    assert status == 404
    assert "text/html" in ctype
    assert "找不到页面" in body.decode("utf-8", "replace")


def test_live_api_errors_are_json():
    """真机上再确认一次：接口错误必须是 JSON，不能被代理或服务器改成 HTML."""
    _server()
    status, body, ctype = _get("/api/no-such-endpoint-smoke")
    assert status == 404
    assert "application/json" in ctype, ctype
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is False and payload["error"]


def test_live_concurrent_requests_stay_healthy():
    """Waitress 是 8 线程；并发下不允许出现 5xx 或连接错误."""
    _server()
    paths = ["/", "/projects", "/servers", "/settings", "/expiries", "/audit"]

    def hit(i: int):
        p = paths[i % len(paths)]
        try:
            status, _, _ = _get(p, timeout=40)
            return status
        except Exception as err:  # noqa: BLE001
            return f"ERR:{type(err).__name__}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(hit, range(48)))

    bad = [r for r in results if not isinstance(r, int) or r >= 500]
    assert not bad, f"并发下出现异常：{bad[:5]}"
