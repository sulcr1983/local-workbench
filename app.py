"""Supersu 工作台 · 启动入口.

只做四件事：查端口 → 创建 app → 起 Waitress → 退出时收尾。
业务代码一律在 wb/ 里，这里不许长（上限 150 行，见 TECH_SPEC §3）。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser

HOST = "127.0.0.1"
# 默认 8787；换端口用 WB_PORT，方便同时开两个实例做对比
PORT = int(os.environ.get("WB_PORT") or 8787)


def port_in_use(host: str, port: int) -> bool:
    """端口已有服务在监听 —— 多半是工作台自己已经开着了."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def main() -> int:
    if port_in_use(HOST, PORT):
        print()
        print(f"  [!] 端口 {PORT} 已经被占用。")
        print(f"      如果工作台已经开着，直接用浏览器访问 http://{HOST}:{PORT}/")
        print("      如果是别的程序占了，关掉它再启动。")
        print()
        return 2

    # 延迟导入：缺依赖时报错发生在打印提示之后，用户能看懂上下文
    from waitress import serve

    from wb import APP_NAME, create_app
    from wb.db import DBError

    try:
        app = create_app()
    except DBError as err:
        print()
        print("  [!] 数据库初始化失败，工作台没有启动。")
        for line in str(err).splitlines():
            print(f"      {line}")
        print()
        return 3

    # 定时巡检与到期检查：WB_NO_SCHED=1 可关（测试/演示时用）
    from wb import scheduler

    try:
        scheduler.autostart()
    except Exception as err:  # noqa: BLE001 - 调度起不来不该阻断页面
        print(f"  [!] 定时任务没启动：{err}（页面功能不受影响）")

    print()
    print(f"  {APP_NAME}")
    print(f"  地址：http://{HOST}:{PORT}/")
    print("  关闭本窗口即停止（或按 Ctrl+C）")
    print()

    if os.environ.get("WB_NO_BROWSER") != "1":
        threading.Timer(0.8, webbrowser.open, args=(f"http://{HOST}:{PORT}/",)).start()

    try:
        serve(app, host=HOST, port=PORT, threads=8)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n  已停止。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
