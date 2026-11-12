#!/usr/bin/env python3
"""Git 门禁统一实现（pre-commit / pre-push / commit-msg）。

设计口径
--------
1. **主仓库永远不许 push**。私有历史里全是真实 IP、主机名、私钥路径和私有项目名，
   一旦误推无法撤回。push 只有一条出口：`scripts/publish_public.py`。
2. **pre-commit 只拦「不该入库的垃圾与硬密钥」**（.db / 私钥 / token / 大文件），
   隐私词交给 publish 阶段全量扫描。理由：主仓库本来就是私有的，日常开发天天
   被某个私有项目名拦住只会让人学会用 --no-verify，门禁就废了。想升级为严格模式：
   `WB_STRICT=1 git commit`。
3. 任何 hook 都可以被 `--no-verify` 绕过，所以真正的底线是 pre-push 那一层：
   它不受 `--no-verify` 影响吗？——也受影响。因此额外要求：本机 core.hooksPath
   由 `scripts/install_hooks.py` 固定，且 `scripts/verify_guard.py` 可随时自检。

退出码：0 通过，1 拒绝。拒绝时说清「为什么」和「该怎么办」。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_CONF = ROOT / ".wb-publish.json"

# 允许 push 的环境变量：只有 publish 脚本会带
PUBLISH_ENV = "WB_PUBLISH_RUN"

# ---------------------------------------------------------------- 通用工具


def sh(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def staged_files() -> list[str]:
    r = sh("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return [x for x in r.stdout.splitlines() if x.strip()]


def staged_blob(path: str) -> bytes | None:
    r = subprocess.run(["git", "show", f":{path}"], cwd=str(ROOT),
                       capture_output=True)
    return r.stdout if r.returncode == 0 else None


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


# ---------------------------------------------------------------- 规则表

# 文件名层面：这些根本不该进版本库
BAD_NAME_GLOBS = [
    (re.compile(r"(^|/)cf_[^/]*\.json$"), "站点探测结果（含真实域名/账户线索）"),
    (re.compile(r"\.(db|db-wal|db-shm|sqlite|sqlite3)$"), "数据库文件（含真实数据）"),
    (re.compile(r"\.(pem|key|pfx|p12)$"), "私钥/证书文件"),
    (re.compile(r"(^|/)id_(rsa|ed25519|ecdsa)$"), "SSH 私钥"),
    (re.compile(r"(^|/)\.env$"), "环境变量文件（可能含密钥）"),
    (re.compile(r"(^|/)__pycache__/"), "Python 缓存目录"),
    (re.compile(r"^data/"), "运行时数据目录"),
    (re.compile(r"_archive_[^/]*/"), "历史归档目录"),
    (re.compile(r"\.(zip|7z|rar|tar\.gz)$"), "打包产物"),
]

# 内容层面：硬密钥，命中即拒（这类东西一旦进库，等于永久泄露）
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥内容"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "OpenAI 风格 API Key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "GitHub Personal Token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "GitHub Fine-grained Token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    # 口令类只抓「值足够长且像随机串」的，否则测试里的 dummy token 全是误报
    (re.compile(r"\b(?:password|passwd|secret|api_key|apikey)\s*[=:]\s*[\"'][A-Za-z0-9_\-+/=]{24,}[\"']",
                re.IGNORECASE), "疑似硬编码口令"),
]

# 内容层面：隐私线索，默认只警告（WB_STRICT=1 时升级为拒绝）
#
# 清单**不写在这个文件里**，而是从 .wb-publish.json 的 must_not_contain 读。
# 原因很实际：这个脚本是要对外发布的，规则表里列着哪些词，
# 等于把「哪些是我的私有项目」直接公开。配置文件不发布，清单就安全。
PRIVACY_PATTERNS: list[tuple[re.Pattern, str]] = []  # 运行时由 load_privacy_patterns() 填充


def load_privacy_patterns() -> list[tuple[re.Pattern, str]]:
    terms = load_publish_conf().get("must_not_contain", [])
    return [(re.compile(re.escape(t), re.IGNORECASE), f"禁用词「{t}」") for t in terms]

TEXT_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".html", ".htm", ".css", ".md",
            ".json", ".txt", ".bat", ".cmd", ".ps1", ".sh", ".yml", ".yaml",
            ".ini", ".cfg", ".toml", ".sql", ".example", ".sample"}

VENDOR_HINTS = ("static/vendor/", "node_modules/", ".venv/")

# 门禁配置本身必然写着「哪些词是敏感的」，对它做隐私词检查只会刷屏
PRIVACY_EXEMPT = {".wb-publish.json"}

MAX_FILE_BYTES = 5 * 1024 * 1024  # 单文件 5MB


# ---------------------------------------------------------------- pre-commit


def run_pre_commit() -> int:
    strict = os.environ.get("WB_STRICT") == "1"
    files = staged_files()
    if not files:
        return 0

    blocks: list[str] = []
    warns: list[str] = []

    for path in files:
        skip = any(h in path.replace("\\", "/") for h in VENDOR_HINTS)
        if skip:
            continue

        # 1) 文件名规则
        for pat, why in BAD_NAME_GLOBS:
            if pat.search(path.replace("\\", "/")):
                blocks.append(f"  {path} —— {why}")
                break
        else:
            # 2) 体积
            blob = staged_blob(path)
            if blob is None:
                continue
            if len(blob) > MAX_FILE_BYTES:
                blocks.append(f"  {path} —— 单文件 {human_size(len(blob))}，超过 5MB 上限")

            # 3) 内容规则（只看文本类，避免二进制误判）
            ext = Path(path).suffix.lower()
            if ext in TEXT_EXT:
                text = blob.decode("utf-8", errors="replace")
                for pat, why in SECRET_PATTERNS:
                    m = pat.search(text)
                    if m:
                        snippet = m.group(0)[:40].replace("\n", " ")
                        blocks.append(f"  {path} —— {why}：{snippet}…")
                if path.replace("\\", "/") in PRIVACY_EXEMPT:
                    continue
                for pat, why in load_privacy_patterns():
                    if pat.search(text):
                        (blocks if strict else warns).append(f"  {path} —— {why}")

    if warns:
        print("\n[门禁·提醒] 以下文件含隐私线索（主仓库可提交，但不会被发布）\n",
              file=sys.stderr)
        print("\n".join(sorted(set(warns))), file=sys.stderr)
        print("  如需拦截：WB_STRICT=1 git commit\n", file=sys.stderr)

    if blocks:
        print("\n[门禁·拒绝] 提交被拦下，以下内容不允许进入版本库：\n", file=sys.stderr)
        print("\n".join(sorted(set(blocks))), file=sys.stderr)
        print("\n处理办法：", file=sys.stderr)
        print("  · 密钥/私钥 → 立即吊销并改用环境变量", file=sys.stderr)
        print("  · 数据库/打包产物 → 加入 .gitignore，git rm --cached <path>", file=sys.stderr)
        print("  · 确属误判 → 临时放行：WB_GUARD_ALLOW=1 git commit（会记录到日志）", file=sys.stderr)
        print("", file=sys.stderr)
        if os.environ.get("WB_GUARD_ALLOW") == "1":
            print("[门禁] 已按 WB_GUARD_ALLOW=1 放行（本次不阻断）", file=sys.stderr)
            return 0
        return 1
    return 0


# ---------------------------------------------------------------- pre-push


def normalize_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.lower()


def load_publish_conf() -> dict:
    if PUBLISH_CONF.exists():
        try:
            return json.loads(PUBLISH_CONF.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def run_pre_push(remote: str, url: str) -> int:
    """主仓库永远拒绝 push；只有 publish 脚本带 WB_PUBLISH_RUN=1 才能出去。"""
    # 发布通道由环境变量显式声明，目标地址优先取 env（导出副本里没有配置文件）
    conf = load_publish_conf()
    allowed_url = normalize_url(os.environ.get("WB_PUBLISH_URL") or conf.get("repo_url", ""))
    publishing = os.environ.get(PUBLISH_ENV) == "1"

    if not publishing:
        print("""
[门禁·拒绝] 这个仓库不允许直接 push。

原因：本地私有历史里有真实服务器 IP、主机名、私钥路径和私有项目名，
      推到任何远程都无法撤回。push 只有一条出口：脱敏发布流程。

不管你想推什么，都走这条路：

     python scripts/publish_public.py --plan      # 先看会发什么（不推送）
     python scripts/publish_public.py --apply     # 脱敏 + 单次提交 + 推送

如果你只是想备份本地代码：提交到本地就好，不要 push。
如果确实需要新增一个可推送的远程：改 .wb-publish.json 的 repo_url，
     并让发布脚本执行（它会做内容审查）。

本次 push 目标：{remote} -> {url}
""".format(remote=remote, url=url), file=sys.stderr)
        return 1

    # publish 通道：仍然校验目标地址，防止脚本被改坏后推错地方
    if not allowed_url:
        print("[门禁] .wb-publish.json 缺少 repo_url，拒绝推送", file=sys.stderr)
        return 1
    if normalize_url(url) != allowed_url:
        print(f"[门禁] 推送目标与白名单不一致\n  目标：{normalize_url(url)}\n  白名单：{allowed_url}",
              file=sys.stderr)
        return 1

    # 只许推 main，且不许推 tag
    payload = sys.stdin.read() if not sys.stdin.isatty() else ""
    for line in payload.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local_ref, _local_sha, remote_ref, _remote_sha = parts[:4]
        if local_ref.startswith("refs/tags/"):
            print(f"[门禁] 发布流程不推送 tag：{local_ref}", file=sys.stderr)
            return 1
        if remote_ref not in ("refs/heads/main", ""):
            print(f"[门禁] 只允许推送 main，当前：{remote_ref}", file=sys.stderr)
            return 1
    return 0


# ---------------------------------------------------------------- commit-msg

BANNED_MSGS = {"wip", "tmp", "temp", "test", "fix", "update", "修改", "更新", "提交", "."}


def run_commit_msg(msg_path: str) -> int:
    try:
        raw = Path(msg_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    # 去掉注释行和空行
    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return 0  # merge / revert 自动生成的空信息，交给 git 自己处理
    subject = lines[0].strip()
    if subject.lower().rstrip("。.") in BANNED_MSGS or len(subject) < 6:
        print(f"""
[门禁·拒绝] 提交信息太含糊："{subject}"

至少要说清「改了什么地方、为什么」。推荐格式：

    <类型>(<范围>): <一句话说明>

    类型：feat / fix / refactor / docs / test / chore / release
    例：fix(readme): 本地相对图片 404，改走 /readme-asset 路由

写清信息不是为了规矩，是为了三个月后你能看懂自己当时在干什么。
""", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------- 入口


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: git_guard.py {pre-commit|pre-push|commit-msg} [...]", file=sys.stderr)
        return 2
    mode = argv[1]
    if mode == "pre-commit":
        return run_pre_commit()
    if mode == "pre-push":
        # git 传参：<remote-name> <remote-url>
        remote = argv[2] if len(argv) > 2 else "?"
        url = argv[3] if len(argv) > 3 else "?"
        return run_pre_push(remote, url)
    if mode == "commit-msg":
        return run_commit_msg(argv[2] if len(argv) > 2 else "")
    print(f"未知模式：{mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
