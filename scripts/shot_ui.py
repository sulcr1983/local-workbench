"""有头 Chrome 截图 + DOM 几何检查.

为什么不用无头 / 不引 Playwright：
- 项目铁律：UI 验收必须有头浏览器实测（无头下 rAF 节流、字体度量都会骗人）。
- Chrome 自带 CDP，websocket-client 一行能通，不必为截图拉一个浏览器运行时。

用法：
    python scripts/shot_ui.py http://127.0.0.1:8787/ docs/assets/screenshots/x.png
    python scripts/shot_ui.py --multi pages.txt        # 每行 "url|输出路径"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket  # websocket-client

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
PORT = 9333

GEOM_JS = r"""
(() => {
  const g = (s) => {
    const e = document.querySelector(s);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  };
  const sizes = {};
  document.querySelectorAll('body *').forEach((e) => {
    if (!e.textContent || !e.textContent.trim()) return;
    const fs = getComputedStyle(e).fontSize;
    sizes[fs] = (sizes[fs] || 0) + 1;
  });
  return JSON.stringify({
    url: location.pathname,
    title: document.title,
    rail: g('.wb-rail'),
    main: g('.wb-main'),
    top: g('.wb-top'),
    wrap: g('.wb-wrap'),
    card: g('.wb-card'),
    h1: g('.wb-ph h1'),
    bodyW: document.body.scrollWidth,
    bodyH: document.body.scrollHeight,
    fontSizes: sizes,
  });
})()
"""


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("没找到 Chrome / Edge，无法做有头验收")


class Browser:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.ws = None
        # 先清上一次异常退出留下的 profile：堆太多 Chrome 会连调试端口都起不来
        self.sweep_leftovers()
        self.profile = tempfile.mkdtemp(prefix="wb-shot-")

    def start(self, url: str) -> None:
        # Chrome 冷启动偶尔 25s 都露不出调试端口（扩展/首启动索引），
        # 自动重来一次比重跑整个脚本省事。
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                self._launch(url)
                return
            except Exception as err:  # noqa: BLE001
                last_err = err
                self._kill_proc()
                # 失败的这一轮也会建 profile 目录，换新之前先把它清掉，
                # 否则每次重试都在 %TEMP% 里留一份垃圾
                self._clean_profile()
                self.profile = tempfile.mkdtemp(prefix="wb-shot-")
                time.sleep(1.0)
        raise SystemExit(f"Chrome 起不来（试了 2 次）：{last_err}")

    def _launch(self, url: str) -> None:
        self.proc = subprocess.Popen(
            [
                find_chrome(),
                f"--remote-debugging-port={PORT}",
                f"--user-data-dir={self.profile}",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                # 本机环境有 http_proxy / https_proxy（Clash 那类），Chrome 会继承，
                # 于是连 127.0.0.1:8787 都往代理送 → 加载成 chrome-error://chromewebdata，
                # 截图全白、DOM 查询全空，看着像"页面坏了"，其实请求根本没到本机服务。
                "--no-proxy-server",
                "--disable-features=Translate",
                "--window-size=1440,900",
                "--window-position=60,60",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ws_url = self._wait_target()
        self.ws = websocket.create_connection(ws_url, timeout=40)
        self._cmd("Page.enable")
        self._cmd("Page.bringToFront")
        # 显式锁定视口：窗口被遮挡或未激活时，不覆盖会拿不到渲染表面
        self._cmd(
            "Emulation.setDeviceMetricsOverride",
            width=1440,
            height=900,
            deviceScaleFactor=1,
            mobile=False,
        )
        time.sleep(1.2)  # 让字体、动画落定

    def _kill_proc(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None
        if self.proc:
            # 只 terminate 父进程不够：Chrome 是多进程的，渲染器 / GPU 子进程
            # 还握着 profile 里的文件句柄，导致 rmtree 删不干净、越积越多
            # （实测堆积到 119 个之后连调试端口都起不来）。必须整棵树杀掉。
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        # taskkill /T 偶尔漏掉已被 reparent 的孙进程，再按命令行特征兜一遍。
        # 这一步是「用完必须不留进程」的保险，不做的话机器会被拖卡。
        Browser.kill_stray_chrome()

    def _wait_target(self) -> str:
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=1) as r:
                    for t in json.loads(r.read()):
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            return t["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.25)
        raise SystemExit("Chrome 调试端口没起来（25s 超时）")

    def _cmd(self, method: str, **params):
        self._id = getattr(self, "_id", 0) + 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败: {msg['error']}")
                return msg.get("result", {})

    def goto(self, url: str) -> None:
        self._cmd("Page.navigate", url=url)
        time.sleep(1.4)

    def shot(self, out: str, full: bool = False) -> None:
        if full:
            # 先把视口拉到整页高度，否则底部的分页条截不进来
            h = self._cmd(
                "Runtime.evaluate",
                expression="Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)",
                returnByValue=True,
            )["result"]["value"]
            self._cmd(
                "Emulation.setDeviceMetricsOverride",
                width=1440,
                height=int(min(int(h) + 8, 6000)),
                deviceScaleFactor=1,
                mobile=False,
            )
            time.sleep(0.45)
        res = self._cmd("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "wb") as f:
            f.write(base64.b64decode(res["data"]))

        if full:
            # 必须还原视口，否则下一张图的 bodyH 是在被撑大的视口里量的（会逐页 +8 漂移）
            self._cmd(
                "Emulation.setDeviceMetricsOverride",
                width=1440,
                height=900,
                deviceScaleFactor=1,
                mobile=False,
            )
            time.sleep(0.2)

    def geom(self) -> dict:
        res = self._cmd(
            "Runtime.evaluate", expression=GEOM_JS, returnByValue=True, awaitPromise=False
        )
        return json.loads(res["result"]["value"])

    def close(self) -> None:
        self._kill_proc()
        # 必须删掉临时 profile：不删的话每跑一次就在 %TEMP% 留一个几百 MB 的目录，
        # 堆到上百个之后 Chrome 连调试端口都起不来（实际踩过：119 个残留）。
        self._clean_profile()

    def _clean_profile(self, path: str | None = None) -> None:
        import shutil

        target = path or self.profile
        if not target:
            return
        # Chrome 退出后文件句柄要几百毫秒才释放，等一会儿再删 —— 删早了留下半截目录，
        # 下次启动时 Chrome 见到残缺 profile 又起不来，越积越多。
        for delay in (0.2, 0.4, 0.8, 1.2, 1.6):
            time.sleep(delay)
            try:
                shutil.rmtree(target, ignore_errors=False)
                if path is None:
                    self.profile = None
                return
            except (OSError, PermissionError):
                continue
        # 真删不掉也不能让脚本挂掉
        if path is None:
            self.profile = None

    # 本脚本启动的 Chrome，命令行里一定有这两个特征之一。
    # 用户自己开的 Chrome 没有，所以按这个杀**不会误伤**。
    _MY_MARKERS = ("wb-shot-", f"remote-debugging-port={PORT}")

    @staticmethod
    def kill_stray_chrome() -> int:
        """杀掉本脚本残留的 Chrome 进程树，返回杀掉的个数.

        为什么必须这么做：Chrome 是多进程的，`proc.terminate()` 只杀父进程，
        渲染器 / GPU 子进程会一直活着 —— 握着 profile 的文件句柄（删不掉），
        还占着几百 MB 内存。实际踩过：累计 40 个进程 / 3.1GB，机器直接卡。

        清理按**命令行特征**匹配，只认带 `wb-shot-` 或调试端口的进程，
        用户自己开的 Chrome 命令行里没有这些，一个都不会碰。
        （`wmic` 被本机安全策略禁用，所以走 PowerShell 的 CIM 查询。）
        """
        if os.name != "nt":
            return 0
        ps = (
            "$c = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
            "-ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.CommandLine -like '*{Browser._MY_MARKERS[0]}*' -or "
            f"$_.CommandLine -like '*{Browser._MY_MARKERS[1]}*' }}; "
            "$n = @($c).Count; "
            "$c | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
            "Write-Output $n"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return int((r.stdout or "0").strip() or 0)
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0

    @staticmethod
    def sweep_leftovers() -> int:
        """启动时清掉上一次异常退出留下的 profile（25s 超时的元凶就在这里）.

        只动自己建的 `wb-shot-*` 目录，不碰用户的任何文件。
        删不掉说明还有僵尸 Chrome 握着句柄 —— 那些进程也是我们起的，
        按命令行特征精确杀掉，绝不误伤用户自己开的 Chrome。
        """
        import glob
        import shutil

        tmp = tempfile.gettempdir()
        dirs = glob.glob(os.path.join(tmp, "wb-shot-*"))
        if not dirs:
            return 0

        # 先杀残留 Chrome（不杀的话文件句柄还在，下面 rmtree 必然失败）
        Browser.kill_stray_chrome()
        time.sleep(0.4)

        n = 0
        for d in dirs:
            for _ in range(3):
                try:
                    shutil.rmtree(d, ignore_errors=False)
                    n += 1
                    break
                except OSError:
                    time.sleep(0.5)
        return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out", nargs="?", default="docs/assets/screenshots/shot.png")
    ap.add_argument("--full", action="store_true", help="截整页（含底部）")
    args = ap.parse_args()

    b = Browser()
    try:
        b.start(args.url)
        b.goto(args.url)
        b.shot(args.out, full=args.full)
        info = b.geom()
    finally:
        b.close()

    print(json.dumps(info, ensure_ascii=False, indent=2))
    print(f"\n截图：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
