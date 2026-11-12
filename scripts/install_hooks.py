#!/usr/bin/env python3
"""安装 / 自检 Git 门禁。

用法：
    python scripts/install_hooks.py            # 安装（设置 core.hooksPath）
    python scripts/install_hooks.py --check    # 只检查，不改动（CI 或手动自检用）

为什么需要这个脚本而不是手工敲 git config：
hooksPath 属于本地配置，不进版本库；换个克隆、换台机器就得重装。
这个脚本顺带做一次体检，确认三个 hook 都在、都可执行。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = ROOT / ".githooks"
REQUIRED = ["pre-commit", "pre-push", "commit-msg"]


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip()


def ensure_executable() -> None:
    """Windows 不需要 exec 位，但 WSL / macOS / Linux 需要。"""
    for name in REQUIRED:
        p = HOOK_DIR / name
        if not p.exists():
            continue
        mode = p.stat().st_mode
        p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def cmd_install() -> int:
    if not HOOK_DIR.is_dir():
        print(f"[失败] 找不到 hook 目录：{HOOK_DIR}", file=sys.stderr)
        return 1
    ensure_executable()
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"],
                   cwd=str(ROOT), check=True)
    print("[完成] 已启用门禁：core.hooksPath = .githooks")
    return cmd_check()


def cmd_check() -> int:
    ok = True
    hp = git("config", "--get", "core.hooksPath")
    if hp != ".githooks":
        print(f"[未启用] core.hooksPath = {hp or '(空，用默认 .git/hooks)'}")
        print("         修复：python scripts/install_hooks.py")
        ok = False
    else:
        print("[OK] core.hooksPath = .githooks")

    for name in REQUIRED:
        p = HOOK_DIR / name
        if not p.exists():
            print(f"[缺失] .githooks/{name}")
            ok = False
            continue
        executable = bool(p.stat().st_mode & stat.S_IXUSR) or os.name == "nt"
        print(f"[{'OK' if executable else '缺执行位'}] .githooks/{name}")
        ok = ok and executable

    conf = ROOT / ".wb-publish.json"
    if conf.exists():
        try:
            data = json.loads(conf.read_text(encoding="utf-8"))
            if data.get("repo_url"):
                print(f"[OK] 发布白名单 repo_url = {data['repo_url']}")
            else:
                print("[提醒] .wb-publish.json 没配 repo_url —— 发布通道不可用，push 仍全禁")
        except Exception as e:  # noqa: BLE001
            print(f"[损坏] .wb-publish.json：{e}")
            ok = False
    else:
        # 没有白名单 = 谁都不许推，这是安全的默认值，不算失败
        print("[OK] 未配置 .wb-publish.json —— 默认全局禁推（安全默认）")

    remotes = [ln for ln in git("remote", "-v").splitlines() if ln.strip()]
    if remotes:
        print("[提醒] 当前仓库配了远程，push 仍会被 pre-push 拦下，但建议保持干净：")
        for ln in remotes:
            print("       " + ln)
    else:
        print("[OK] 本仓库未配置任何远程（推荐状态）")

    print()
    print("门禁体检：" + ("全部通过" if ok else "有问题，见上"))
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return cmd_check()
    return cmd_install()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
