"""打包发布：白名单收录 + 泄漏断言 + zip + sha256.

为什么用白名单而不是"排除法"：
排除法只要漏写一条规则，data/ 里的密码库就会跟着发出去。
白名单是"没列出来的一律不进包"，漏了顶多少个文件，不会泄密。
"""

from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "release")
VERSION = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
PKG = f"workbench-{VERSION}"
ZIP_PATH = os.path.join(RELEASE, f"{PKG}.zip")

# 单文件白名单
FILES = [
    "app.py",
    "start.bat",
    "requirements.txt",
    "README.md",
    "VERSION",
    "LICENSE",
    "THIRD-PARTY-NOTICES.md",
]
# 整目录收录（递归，但会剔除缓存与运行时产物）
DIRS = ["wb", "templates", "static"]

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".db", ".db-shm", ".db-wal"}

# 一旦出现这些路径，立刻失败 —— 说明包里混进了不该带的
LEAK_PATTERNS = (
    "data/",
    "data\\",
    "config.json",
    "_archive",
    ".venv",
    "browser-profile",
    "workbench.db",
    "wb_known_hosts",
)


def _iter_dir(rel_dir: str):
    base = os.path.join(ROOT, rel_dir)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if any(name.endswith(ext) for ext in SKIP_EXT):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            yield rel, full


def collect() -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for rel in FILES:
        full = os.path.join(ROOT, rel)
        if not os.path.isfile(full):
            raise SystemExit(f"白名单里的文件不存在：{rel}")
        items.append((rel, full))
    for d in DIRS:
        if not os.path.isdir(os.path.join(ROOT, d)):
            raise SystemExit(f"白名单里的目录不存在：{d}")
        items.extend(_iter_dir(d))
    # 使用说明跟着包走，放进根目录方便非技术用户第一眼看到
    readme = os.path.join(RELEASE, "使用说明.txt")
    if os.path.isfile(readme):
        items.append(("使用说明.txt", readme))
    return items


def assert_no_leak(rels: list[str]) -> None:
    bad = []
    for rel in rels:
        low = rel.lower()
        for pat in LEAK_PATTERNS:
            if pat.lower() in low:
                bad.append(f"{rel}  ← 命中 {pat}")
    if bad:
        print("[!] 包里混进了不该发布的东西，已中止：")
        for b in bad:
            print("   ", b)
        raise SystemExit(2)


def main() -> int:
    items = collect()
    assert_no_leak([rel for rel, _ in items])

    os.makedirs(RELEASE, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, full in items:
            zf.write(full, f"{PKG}/{rel}")

    digest = hashlib.sha256(open(ZIP_PATH, "rb").read()).hexdigest()
    sha_path = os.path.join(RELEASE, f"{PKG}.sha256")
    with open(sha_path, "w", encoding="utf-8") as f:
        f.write(f"{digest}  {PKG}.zip\n")

    size = os.path.getsize(ZIP_PATH)
    print(f"打好了：{ZIP_PATH}")
    print(f"  文件数 {len(items)} · 大小 {size / 1024:.0f} KB")
    print(f"  sha256 {digest}")
    print(f"  时间 {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
