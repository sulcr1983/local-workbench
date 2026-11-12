#!/usr/bin/env python3
"""对外发布：从 HEAD 导出干净副本 → 脱敏 → 审查 → 单次提交 → 推送。

这是**唯一**允许 push 的入口。主仓库的 pre-push 会拒绝一切其他来源的推送。

为什么要这么绕：本地私有历史里有真实服务器 IP、主机名、私钥路径和私有项目名，
一旦推到公开仓库就无法撤回。所以对外发布不继承历史，只发「当前代码 + 一份脱敏」，
在导出副本里重新 init 一个单提交仓库。

用法：
    python scripts/publish_public.py              # 干跑：构建并报告，不推送
    python scripts/publish_public.py --apply      # 真的推送
    python scripts/publish_public.py --keep       # 保留构建目录，方便人工检查
    python scripts/publish_public.py --apply -m "自定义提交信息"

退出码：0 成功 / 1 审查不通过或执行失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF_PATH = ROOT / ".wb-publish.json"

sys.path.insert(0, str(ROOT / "scripts"))
try:
    from git_guard import SECRET_PATTERNS
except Exception:  # noqa: BLE001 - 门禁脚本被改动时不该让发布崩掉
    SECRET_PATTERNS = []

TEXT_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".html", ".htm", ".css", ".md",
            ".json", ".txt", ".bat", ".cmd", ".ps1", ".sh", ".yml", ".yaml",
            ".ini", ".cfg", ".toml", ".sql", ".example", ".sample"}
SKIP_DIRS = ("static/vendor/", "node_modules/", "__pycache__/", ".venv/")


def sh(args: list[str], cwd: Path, env: dict | None = None, check: bool = True):
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=e)
    if check and r.returncode != 0:
        print(f"[失败] {' '.join(args)}\n{r.stdout}\n{r.stderr}", file=sys.stderr)
        raise SystemExit(1)
    return r


def load_conf() -> dict:
    if not CONF_PATH.exists():
        print(f"[失败] 找不到 {CONF_PATH}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(CONF_PATH.read_text(encoding="utf-8"))


def head_sha() -> str:
    return sh(["git", "rev-parse", "--short", "HEAD"], ROOT).stdout.strip()


def head_tag_or_version() -> str:
    v = (ROOT / "VERSION")
    return v.read_text(encoding="utf-8").strip() if v.exists() else "dev"


# ---------------------------------------------------------------- 1. 导出


def export_head(dest: Path, conf: dict) -> None:
    """git archive 只含已提交内容，天然把并行任务的未提交改动挡在外面。"""
    paths = list(conf["include_files"]) + list(conf["include_dirs"])
    # 只保留真实存在的路径
    paths = [p for p in paths if (ROOT / p).exists()]
    tar_path = dest.parent / "snapshot.tar"
    sh(["git", "archive", "--format=tar", "-o", str(tar_path), "HEAD", "--", *paths], ROOT)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tf:
        tf.extractall(dest)
    tar_path.unlink(missing_ok=True)


# ---------------------------------------------------------------- 2. 脱敏


def iter_text_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(s in rel for s in SKIP_DIRS):
            continue
        if p.suffix.lower() in TEXT_EXT:
            yield p, rel


def scrub(dest: Path, conf: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    rules = [(r["pattern"], r["replace"], r["why"]) for r in conf["scrub"]]
    for path, rel in iter_text_files(dest):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        original = text
        for pat, repl, why in rules:
            new, n = re.subn(pat, repl, text)
            if n:
                counts[f"{why}"] = counts.get(f"{why}", 0) + n
                text = new
        if text != original:
            path.write_text(text, encoding="utf-8")
    return counts


# ---------------------------------------------------------------- 3. 审查


def audit(dest: Path, conf: dict) -> list[str]:
    """脱敏后的最终检查。任何命中都中止发布——宁可发不出去，也不能发出去。"""
    problems: list[str] = []
    musts = conf.get("must_not_contain", [])

    for path, rel in iter_text_files(dest):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for kw in musts:
            for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line} 命中禁用词「{kw}」")
        for pat, why in SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line} 疑似密钥（{why}）")

    # 二进制与体积
    for p in dest.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(dest).as_posix()
        if p.suffix.lower() in {".db", ".zip", ".pem", ".key", ".pfx", ".p12"}:
            problems.append(f"{rel} 不该出现在发布包里")
        if p.stat().st_size > 5 * 1024 * 1024:
            problems.append(f"{rel} 超过 5MB")
    return problems


# ---------------------------------------------------------------- 4. 组装


def stage_extras(dest: Path, conf: dict) -> list[str]:
    notes = []
    readme_src = ROOT / conf["readme_source"]
    if readme_src.exists():
        shutil.copy2(readme_src, dest / "README.md")
        notes.append(f"README ← {conf['readme_source']}")
    else:
        print(f"[失败] 缺少对外 README：{conf['readme_source']}", file=sys.stderr)
        raise SystemExit(1)

    gi_src = ROOT / conf.get("gitignore_source", "")
    if gi_src and gi_src.exists():
        shutil.copy2(gi_src, dest / ".gitignore")
        notes.append(f".gitignore ← {conf['gitignore_source']}")

    shots_src = ROOT / conf.get("screenshot_source", "")
    if shots_src and shots_src.is_dir():
        target = dest / conf.get("screenshot_target", "docs/screenshots")
        target.mkdir(parents=True, exist_ok=True)
        n = 0
        for img in shots_src.iterdir():
            if img.is_file() and img.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                shutil.copy2(img, target / img.name)
                n += 1
        notes.append(f"截图 {n} 张 ← {conf['screenshot_source']}")
    return notes


def commit_and_push(dest: Path, conf: dict, message: str, apply: bool) -> str:
    url = conf["repo_url"]
    branch = conf.get("branch", "main")

    sh(["git", "init", "-b", branch, "-q"], dest)

    # 发布副本也要走一遍门禁：先确认 hook 真的在，不然 core.hooksPath 指向
    # 一个不存在的目录时 git 会静默跳过——门禁看起来配了，实际没跑。
    hooks = dest / ".githooks"
    missing = [n for n in ("pre-commit", "pre-push", "commit-msg")
               if not (hooks / n).is_file()]
    if missing:
        print(f"[失败] 发布副本缺少 hook：{', '.join(missing)}"
              f"\n       检查 .wb-publish.json 的 include_dirs 是否包含 .githooks", file=sys.stderr)
        raise SystemExit(1)
    sh(["git", "config", "core.hooksPath", ".githooks"], dest)
    sh(["git", "config", "user.name", "Supersu"], dest)
    sh(["git", "config", "user.email", "supersu@local"], dest)
    sh(["git", "add", "-A"], dest)
    sh(["git", "commit", "-q", "-m", message], dest)
    sha = sh(["git", "rev-parse", "--short", "HEAD"], dest).stdout.strip()
    files = sh(["git", "ls-files"], dest).stdout.splitlines()

    if not apply:
        return f"未推送（干跑）。本地单提交 {sha}，{len(files)} 个文件"

    sh(["git", "remote", "add", "origin", url], dest)
    # 必须 --force：发布副本每次都是全新 init 的单提交仓库，与远程上一个快照
    # 没有共同祖先，普通 push 会被 "fetch first" 拒绝。发布语义就是「用当前
    # 代码替换远程那份快照」，覆盖是预期行为，不是事故。
    r = sh(["git", "push", "--force", "-u", "origin", branch], dest,
           env={"WB_PUBLISH_RUN": "1", "WB_PUBLISH_URL": url}, check=False)
    if r.returncode != 0:
        print(r.stdout, file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        return f"推送失败（见上）"
    return f"已推送 {url} @ {sha}，{len(files)} 个文件"


# ---------------------------------------------------------------- 主流程


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="对外发布（唯一允许 push 的入口）")
    ap.add_argument("--apply", action="store_true", help="真的推送；不加只干跑")
    ap.add_argument("--keep", action="store_true", help="保留构建目录供人工检查")
    ap.add_argument("-m", "--message", default="", help="自定义提交信息")
    args = ap.parse_args(argv)

    conf = load_conf()
    version = head_tag_or_version()
    sha = head_sha()

    workdir = Path(tempfile.mkdtemp(prefix="wb_publish_"))
    dest = workdir / "repo"
    print(f"发布目标：{conf['repo_url']}  分支 {conf.get('branch', 'main')}")
    print(f"源快照：HEAD {sha}  VERSION {version}\n")

    try:
        print("[1/5] 从 HEAD 导出（不含未提交改动、不含历史）…")
        export_head(dest, conf)
        n_files = sum(1 for _ in dest.rglob("*") if _.is_file())
        print(f"      导出 {n_files} 个文件")

        print("[2/5] 替换对外文案与素材…")
        for note in stage_extras(dest, conf):
            print(f"      {note}")

        print("[3/5] 脱敏…")
        counts = scrub(dest, conf)
        if counts:
            for why, n in sorted(counts.items(), key=lambda x: -x[1]):
                print(f"      {why}：{n} 处")
        else:
            print("      无需替换")

        print("[4/5] 审查…")
        problems = audit(dest, conf)
        if problems:
            print(f"\n[拒绝] 审查发现 {len(problems)} 处问题，已中止，未推送：\n", file=sys.stderr)
            for p in problems[:40]:
                print("  " + p, file=sys.stderr)
            if len(problems) > 40:
                print(f"  …另有 {len(problems) - 40} 处", file=sys.stderr)
            print("\n处理：补 .wb-publish.json 的 scrub 规则，或把该文件移出发布白名单。",
                  file=sys.stderr)
            return 1
        print("      通过（无禁用词、无密钥、无数据库/大文件）")

        print("[5/5] 建单提交…")
        msg = args.message or (
            f"release: {version} 公开快照（源码同步自本地主仓库 HEAD {sha}）\n\n"
            f"本次仅同步代码与文档，不含历史提交。"
        )
        result = commit_and_push(dest, conf, msg, args.apply)
        print(f"      {result}")

        if args.keep:
            print(f"\n构建目录保留在：{dest}")
        print("\n完成。" if args.apply else "\n干跑结束。确认无误后加 --apply 真正推送。")
        return 0
    finally:
        if not args.keep:
            for _ in range(3):
                try:
                    shutil.rmtree(workdir)
                    break
                except PermissionError:
                    for p in workdir.rglob("*"):
                        try:
                            p.chmod(0o700)
                        except OSError:
                            pass
                    time.sleep(0.3)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
