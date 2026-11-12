"""start.bat 端到端：真的 double-click 那一下会发生什么.

前面的 test_launcher.py 只检查文件字节（编码/行尾/探测清单）。这里补上
「真跑一遍」：用子进程启动 start.bat，断言它不重装依赖、不报乱码命令、
并且真把服务拉起来能访问。

为什么值得单列：用户双击后的现象（滚 pip 输出、乱码报错）在这两条线以外，
单测永远看不到。
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOOT_TIMEOUT = 60
_state: dict = {"proc": None, "log": "", "port": 0}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _read_log() -> str:
    try:
        with open(_state["log"], "rb") as f:
            return f.read().decode("gbk", "replace")
    except OSError:
        return ""


def _stop():
    proc = _state.get("proc")
    if proc is not None and proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    _state["proc"] = None


atexit.register(_stop)


def _launch() -> int:
    if _state["proc"] is not None and _state["proc"].poll() is None:
        return _state["port"]

    port = _free_port()
    log_path = os.path.join(tempfile.mkdtemp(prefix="wb-bat-"), "out.log")
    env = dict(os.environ)
    env.update({
        "WB_PORT": str(port),
        "WB_DB": log_path.replace(".log", ".db"),
        "WB_NO_SCHED": "1",
        "WB_NO_BROWSER": "1",     # 测试时不弹浏览器
    })

    log = open(log_path, "wb")
    proc = subprocess.Popen(["cmd", "/c", "start.bat"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    _state.update({"proc": proc, "log": log_path, "port": port})
    return port


def _wait_ready(port: int) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        proc = _state["proc"]
        if proc is not None and proc.poll() is not None:
            break
        try:
            with opener.open(f"http://127.0.0.1:{port}/", timeout=3) as resp:
                if resp.status in (200, 302):
                    return
        except urllib.error.HTTPError as err:
            if err.code in (200, 302):
                return
        except Exception:  # noqa: BLE001 - 还没起来
            pass
        time.sleep(0.3)
    raise AssertionError(
        f"start.bat 在 {BOOT_TIMEOUT}s 内没把服务拉起来。输出：\n{_read_log()[-2000:]}")


# ---------------------------------------------------------------- 用例


def test_bat_launches_server_without_reinstalling_deps():
    """核心回归：双击后必须直奔启动，不许再全量 pip install."""
    port = _launch()
    _wait_ready(port)
    out = _read_log()

    assert "正在安装依赖" not in out, f"又去装依赖了：\n{out[-1500:]}"
    assert "Requirement already satisfied" not in out, f"跑了 pip：\n{out[-1500:]}"


def test_bat_output_has_no_garbled_command_errors():
    """GBK/CRLF 一旦被改坏，cmd 会把中文行截断成乱码命令，这里必须炸."""
    port = _launch()
    _wait_ready(port)
    out = _read_log()

    for bad in ("不是内部或外部命令", "is not recognized", "系统找不到指定的路径"):
        assert bad not in out, f"cmd 解析出错（出现「{bad}」）：\n{out[-1500:]}"


def test_bat_reaches_serving_stage():
    port = _launch()
    _wait_ready(port)
    out = _read_log()
    assert "正在启动" in out or "Supersu" in out, f"没走到启动阶段：\n{out[-1500:]}"
