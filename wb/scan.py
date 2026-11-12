"""目录扫描 + 命名解析 + 活跃度.

三个纯函数是核心，都能单独测：
    resolve_name(path) -> (name, src, conf)
    detect_stack(path) -> str
    active_level(ts, rules) -> str

扫描纪律（TECH_SPEC §10）：
- 命中项目就停止下探，别钻进 node_modules。
- 全量 50 个目录 ≤ 5s：git 调用并发跑，单条命令 2s 超时。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

# 一律不当作项目、也不下探的目录
EXCLUDE_DIRS = {
    "node_modules", ".venv", "venv", "env", "__pycache__", ".git", ".hg", ".svn",
    "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit", ".cache",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", "site-packages",
    "bower_components", ".tox", "coverage", ".gradle", ".dart_tool",
    "browser-profile", "$RECYCLE.BIN", "System Volume Information",
}

README_NAMES = ("README.md", "readme.md", "Readme.md", "README.MARKDOWN", "README.markdown", "README.txt", "README")

# 有这些东西 = 这是一个项目（不再下探）
PROJECT_MARKERS = (
    ".git", "package.json", "pyproject.toml", "requirements.txt", "Pipfile",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "composer.json",
    "Gemfile", "CMakeLists.txt", ".project", "deno.json", "bun.lockb",
)

# 依赖清单（各语言生态的「这是个工程」声明）。比 README 更能说明是项目根。
MANIFEST_FILES = {
    # Python
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "poetry.lock",
    # Node / 前端
    "package.json", "deno.json", "bun.lockb", "pnpm-workspace.yaml",
    # 其他语言
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "Gemfile", "CMakeLists.txt", "mix.exs", "Package.swift",
    "pubspec.yaml", "build.sbt", "project.clj",
}

# ── 项目根目录的特征打分（参考 GitHub 上的工程惯例，不是拍脑袋）──────────────
# 依据：真实的仓库根目录通常同时具备「版本控制 + 说明 + 许可 + 依赖声明 + CI/构建」，
# 单个 README 只是其中一项；反过来，只有源码文件堆在一起的多半是某个项目的子目录。
ROOT_SIGNALS: tuple[tuple[frozenset[str], int, str], ...] = (
    (frozenset({".git"}), 5, "git 仓库根"),
    (frozenset(MANIFEST_FILES), 4, "依赖清单（说明这是个能装能跑的工程）"),
    (frozenset(README_NAMES), 3, "README"),
    (frozenset({"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "LICENCE"}), 2, "许可证"),
    (frozenset({".gitignore", ".gitattributes"}), 2, ".gitignore"),
    (frozenset({".github", ".gitlab-ci.yml", ".circleci", "azure-pipelines.yml"}), 2, "CI 配置（只在根目录）"),
    (frozenset({"Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml"}), 2, "容器配置"),
    (frozenset({"Makefile", "makefile", "justfile", "Taskfile.yml", "Rakefile"}), 1, "构建入口"),
    (frozenset({"CONTRIBUTING.md", "CHANGELOG.md", "CHANGELOG", "CODE_OF_CONDUCT.md",
                "SECURITY.md", ".editorconfig"}), 1, "协作/工程化文件"),
)

# 分数 → 判定。5 分起步才自动收编：单靠 README(3) 或单靠清单(4) 都还差一口气。
SCORE_AUTO = 5
SCORE_CANDIDATE = 3

CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".html", ".vue")

# 「我是某个项目的组成部分」—— 这类名字本身不是项目，是项目内部的目录。
# 之前把 src / tools / scripts 当成独立项目，是把 54 个目录污染成"项目"的主因。
CONTAINER_NAMES = {
    "src", "source", "sources", "lib", "libs", "app", "apps", "core", "internal",
    "pkg", "packages", "package", "module", "modules", "component", "components",
    "tools", "tool", "scripts", "script", "bin", "cmd", "code", "codes",
    "test", "tests", "testing", "spec", "specs", "docs", "doc", "document",
    "assets", "asset", "static", "public", "resources", "res", "staticfiles",
    "templates", "template", "layouts", "partials", "pages", "views", "routes",
    "prototypes", "prototype", "examples", "example", "samples", "sample",
    "demo", "demos", "playground", "sandbox", "scratch", "tmp", "temp",
    "config", "configs", "conf", "settings", "utils", "util", "helpers", "helper",
    "common", "shared", "base", "vendor", "third_party", "external", "deps",
    "output", "outputs", "out", "dist", "release", "artifact", "artifacts",
    "cache", "logs", "log", "data", "db", "database", "migrations",
    "backup", "backups", "old", "archive", "archived", "trash",
    "work", "workspace", "job", "jobs", "task", "tasks",
    "新建文件夹", "new folder", "untitled", "无标题",
}


def _human_named(base: str) -> bool:
    """目录名像人起的（有中文或含大写），而不是 src / tools 这类机器味的通用词."""
    if re.search(r"[\u4e00-\u9fff]", base):
        return True
    return base != base.lower()


# 浏览器 / 应用数据目录的特征文件。Chrome 会在 profile 里生成一个 README，
# 光看「有 README」会把 cf-browser\Default 当成项目 —— 靠这几个文件认出来。
APP_DATA_MARKERS = (
    "Preferences", "Bookmarks", "History", "Local State", "Web Data",
    "Login Data", "Cookies", "Favicons", "Top Sites", "Visited Links",
)


def looks_like_app_data(names: set[str]) -> bool:
    """命中 3 个以上浏览器/应用数据特征文件 → 这是程序数据目录，不是项目."""
    return sum(1 for m in APP_DATA_MARKERS if m in names) >= 3

# README 的首选标题。**两种写法都要认**：
#   1) Markdown ATX 标题：`# 示例英语`
#   2) HTML 标题：`<h1>示例英语</h1>`（居中排版的 README 很常见）
#
# 只认 `#` 会漏掉第二种，然后退而求其次抓到正文里第一个 `##`（比如
# "## 01 一眼看懂" 下面那句安装说明），把一句安装提示当成项目名。
# demo-app 那个项目就是这么被叫成"项目根目录起任意静态服务即可（零构建…）"的。
MD_H1_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", re.MULTILINE)
HTML_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
# 兼容旧名字（外部还在引用）
H1_RE = MD_H1_RE

# 标题里不该出现的东西：badge 图片、HTML 标签、Markdown 链接语法
_TITLE_NOISE = re.compile(
    r"(?:<[^>]+>|!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\([^)]*\))",
)
# 一看就不是标题的句子（安装说明、运行命令、免责声明）
_NOT_A_TITLE = re.compile(
    r"(?:^|\s)(?:npm|yarn|pnpm|pip|python|node|docker|git|cd|curl|brew)\s",
    re.IGNORECASE,
)
_NOT_A_TITLE_HINT = re.compile(
    r"(?:安装|运行|启动|构建|部署|零构建|任意静态|使用方法|快速开始|quick\s*start|getting\s*started)",
    re.IGNORECASE,
)


def _clean_title(raw: str) -> str:
    """标题文本清洗：去图片、去链接语法、去 HTML 标签、去多余空白.

    顺序有讲究：先剥图片 `![alt](url)`（里面可能还套链接），再剥链接 `[text](url)`
    （保留 text），最后才去裸 HTML 标签。反过来做会留下 `](b)` 这种残渣。
    """
    t = raw or ""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)          # 图片整段丢掉
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)      # 链接只留文字
    t = re.sub(r"<[^>]+>", "", t)                          # HTML 标签
    t = re.sub(r"(?:\*\*|__|\*|_|\`)", "", t)                    # 加粗/斜体/行内代码标记
    t = _TITLE_NOISE.sub("", t)                            # 兜底再扫一遍
    t = re.sub(r"\s+", " ", t).strip().strip("#").strip()
    return t[:120]


def _title_is_usable(title: str) -> bool:
    """这个"标题"配不配当项目名.

    门槛设得很低，只挡住两类明显不该当名字的：
    - 看起来是命令 / 安装说明（含 npm/pip/docker 这类词，或"零构建""任意静态服务"这类措辞）
    - 太长（超过 60 字基本是一句话，不是标题）
    """
    t = (title or "").strip()
    if not t or len(t) < 2:
        return False
    if len(t) > 60:
        return False
    if _NOT_A_TITLE.search(t):
        return False
    if _NOT_A_TITLE_HINT.search(t):
        return False
    return True


def _readme_title(text: str) -> str:
    """从 README 头部找项目标题：先看 <h1>，再看 Markdown # 标题，取第一个"像标题"的.

    顺序很重要：很多 README 用 `<div align="center">` 包一个 `<h1>`，
    然后正文里第一个 `##` 才是章节标题。按"谁在前面取谁"会取错。
    """
    head = (text or "")[:20000]

    # 1) HTML h1（含居中排版）
    for m in HTML_H1_RE.finditer(head):
        title = _clean_title(m.group(1))
        if _title_is_usable(title):
            return title

    # 2) Markdown 一级标题
    for m in MD_H1_RE.finditer(head):
        title = _clean_title(m.group(1))
        if _title_is_usable(title):
            return title

    return ""

DEFAULT_RULES = {"active": 7, "slow": 30, "stale": 90}


@dataclass
class ProjectInfo:
    path: str
    dirname: str
    name: str
    name_src: str
    name_conf: float
    state: str = "auto"          # auto 自动收编 / candidate 候选待定 / dropped 已被引擎减掉
    stack: str = "未识别"
    active: str = "未知"
    is_git: bool = False
    last_commit: str | None = None
    dirty: bool = False
    dirty_n: int = 0
    unpushed_n: int = 0
    remote: str = ""
    summary: str = ""
    preview: str | None = None
    touched: str | None = None
    files: int = 0
    extra: dict = field(default_factory=dict)


# ------------------------------------------------------------------ 读文件
def _read_head(path: str, limit: int = 8192) -> str:
    """读文件头；编码不确定，先 utf-8 再退回 gbk —— 不许因为一个 emoji 崩掉整次扫描."""
    try:
        with open(path, "rb") as f:
            raw = f.read(limit)
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _find_readme(path: str) -> str | None:
    for name in README_NAMES:
        p = os.path.join(path, name)
        if os.path.isfile(p):
            return p
    return None


def find_readme(path: str) -> str | None:
    """公开版：详情页要用它判断有没有 README."""
    return _find_readme(path)


def read_text(path: str, limit: int = 512 * 1024) -> str:
    """读文本（README 最长 512KB，再长就不像 README 了）."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw = f.read(min(size, limit))
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def git_commits(path: str, n: int = 15) -> list[dict]:
    """最近 n 条提交；不是 git 仓库就返回空列表.

    **注意：这是"最近 n 条"，不是"全部"。**
    任何按时间聚合的统计（比如提交节奏的逐周计数）都不能用它 ——
    一个活跃项目一天就有 25 条，取 15 条会把那天算少。
    那种场景请用 `git_commits_since()`。
    """
    if not os.path.isdir(os.path.join(path, ".git")):
        return []
    out = _git(path, "log", f"-{n}", "--format=%h\x1f%ad\x1f%s", "--date=format:%Y-%m-%d", timeout=3.0)
    if not out:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return commits


def git_commits_since(path: str, days: int = 56, cap: int = 500) -> list[dict]:
    """按**时间窗口**取提交（不是按条数），用于逐周统计.

    之前提交节奏用 `git_commits(path, 15)` 算周计数 —— 口径错了：
    11/05 那天实际有 25 条提交，但只取到最近 10 条，柱子上写 10；
    而 11/11 的 5 条全取到了。两边的分母根本不是一个东西。

    这里用 `--since` 让 git 自己按时间筛，cap 只是防超大仓库把内存吃光的上限
    （真到 500 条会返回 truncated=True，界面要说明）。
    """
    if not os.path.isdir(os.path.join(path, ".git")):
        return []
    out = _git(
        path, "log", f"--since={days} days ago", f"--max-count={cap + 1}",
        "--format=%h\x1f%ad\x1f%s", "--date=format:%Y-%m-%d", timeout=5.0,
    )
    if not out:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return commits


# -------------------------------------------------------------- 命名解析
def _prettify(dirname: str) -> str:
    """目录名 → 人看的名字；中文原样保留，纯英文小写才首字母大写."""
    s = re.sub(r"[-_]+", " ", dirname).strip()
    s = re.sub(r"\s{2,}", " ", s)
    if not s:
        return dirname
    if re.fullmatch(r"[a-z0-9 .]+", s):
        return " ".join(w[:1].upper() + w[1:] for w in s.split(" "))
    return s


def _manifest_name(path: str) -> str | None:
    """package.json / pyproject.toml / Cargo.toml / go.mod 里的 name."""
    pkg = os.path.join(path, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8", errors="ignore") as f:
                name = json.load(f).get("name")
            if name:
                return str(name)
        except (OSError, json.JSONDecodeError):
            pass

    py = os.path.join(path, "pyproject.toml")
    if os.path.isfile(py):
        text = _read_head(py, 4096)
        m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if m:
            return m.group(1)

    cargo = os.path.join(path, "Cargo.toml")
    if os.path.isfile(cargo):
        m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', _read_head(cargo, 4096), re.MULTILINE)
        if m:
            return m.group(1)

    gomod = os.path.join(path, "go.mod")
    if os.path.isfile(gomod):
        m = re.search(r"^module\s+(\S+)", _read_head(gomod, 4096), re.MULTILINE)
        if m:
            return m.group(1).rstrip("/").split("/")[-1]

    return None


def resolve_name(path: str) -> tuple[str, str, float]:
    """(名称, 来源, 置信度) —— 0.90 README H1 / 0.72 manifest / 0.45 目录名."""
    readme = _find_readme(path)
    if readme:
        text = _read_head(readme, 20000)
        title = _readme_title(text)
        if title:
            return title, "readme_h1", 0.90

    name = _manifest_name(path)
    if name:
        return name[:120], "manifest", 0.72

    return _prettify(os.path.basename(path.rstrip("\\/")))[:120], "dirname", 0.45


# ------------------------------------------------------------------ 技术栈
STACK_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Python", ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "manage.py")),
    ("Node", ("package.json", "tsconfig.json", "vite.config.ts", "next.config.js")),
    ("Go", ("go.mod", "go.sum")),
    ("Rust", ("Cargo.toml",)),
    ("Java", ("pom.xml", "build.gradle", "build.gradle.kts")),
    (".NET", ("*.csproj", "*.sln")),
    ("PHP", ("composer.json",)),
    ("Ruby", ("Gemfile",)),
]


def detect_stack(path: str) -> str:
    """识别技术栈：先看清单文件，再看源码后缀.

    只看根目录清单会漏掉一大片 —— 很多小项目根本没写 requirements.txt，
    就是一堆散装 .py / .js。这些项目显示「未识别」等于什么都没说，
    而按源码后缀能给出「Python（散装）」这种更有用的答案。
    """
    try:
        names = set(os.listdir(path))
    except OSError:
        return "未识别"

    for label, markers in STACK_RULES:
        for m in markers:
            if m.startswith("*"):
                if any(n.endswith(m[1:]) for n in names):
                    return label
            elif m in names:
                return label

    if "index.html" in names:
        return "静态页"

    # 第二档：没有依赖清单时，按根目录源码后缀判断。
    # 返回值带「（无清单）」而不是「散装」—— 之前那个词是我自己编的，
    # 用户看不懂，也没告诉他任何有用信息。
    by_suffix = _stack_by_suffix(names)
    if by_suffix:
        return by_suffix

    return "未识别"


# 后缀 → 语言。只认根目录，不递归（递归会把 node_modules 里的 js 数进来）
_SUFFIX_STACK: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Python", (".py",)),
    ("Node", (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")),
    ("Go", (".go",)),
    ("Java", (".java", ".kt")),
    ("Rust", (".rs",)),
    ("PHP", (".php",)),
    ("Ruby", (".rb",)),
    ("静态页", (".html", ".htm")),
)


def _stack_by_suffix(names: set[str]) -> str:
    """按根目录源码文件后缀猜语言；一个都没匹配上就返回空串.

    至少要 1 个源码文件才算 —— 只有 README 和图片的目录不该被硬塞一个语言。
    """
    counts: dict[str, int] = {}
    for label, exts in _SUFFIX_STACK:
        n = sum(1 for f in names if f.lower().endswith(exts) and not f.startswith("."))
        if n:
            counts[label] = n
    if not counts:
        return ""
    # 数量最多的胜出；并列时按 _SUFFIX_STACK 顺序（Python 优先，本机最常见）
    best = max(counts.items(), key=lambda kv: (kv[1], -[s[0] for s in _SUFFIX_STACK].index(kv[0])))
    return best[0]


# ------------------------------------------------------------------ 活跃度
def active_level(ts: datetime | None, rules: dict | None = None) -> str:
    """≤7 活跃 / 8–30 放缓 / 31–90 停滞 / >90 可能废弃."""
    if ts is None:
        return "未知"
    r = {**DEFAULT_RULES, **(rules or {})}
    days = (datetime.now() - ts).days
    if days <= r["active"]:
        return "活跃"
    if days <= r["slow"]:
        return "放缓"
    if days <= r["stale"]:
        return "停滞"
    return "可能废弃"


def _git(path: str, *args: str, timeout: float = 2.0) -> str:
    """git 调用一律带超时；失败就是没结果，不抛异常."""
    try:
        out = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", errors="ignore").strip()


def _parse_iso(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def git_info(path: str) -> tuple[bool, datetime | None, int, int, str]:
    """(是否 git, 最后提交, 未提交文件数, 未推送提交数, origin 地址).

    列表页「同步」列要靠这四个数，所以一次算齐，别在模板里再调 git。
    """
    if not os.path.isdir(os.path.join(path, ".git")):
        return False, None, 0, 0, ""
    iso = _git(path, "log", "-1", "--format=%cI")
    status = _git(path, "status", "--porcelain")
    dirty_n = sum(1 for line in status.splitlines() if line.strip())
    unpushed = _git(path, "rev-list", "--count", "@{u}..HEAD")
    try:
        unpushed_n = int(unpushed)
    except (TypeError, ValueError):
        unpushed_n = 0
    remote = _git(path, "remote", "get-url", "origin")
    return True, _parse_iso(iso), dirty_n, unpushed_n, remote


_summary_cache: dict[str, str] = {}


_SSH_ALIAS_CACHE: dict[str, str] | None = None


def _ssh_host_aliases() -> dict[str, str]:
    """~/.ssh/config 的 Host 别名 → 真实 HostName（用 paramiko，不自己造轮子）.

    多账号时 git remote 常写成 `git@github.com-opendoor:...`，这里的
    `github.com-opendoor` 只是 ssh_config 里的别名，不是域名；直接拿去拼
    https 会得到不存在的主机。读不到配置就返回空字典，交给 _web_host 兜底。
    """
    global _SSH_ALIAS_CACHE
    if _SSH_ALIAS_CACHE is not None:
        return _SSH_ALIAS_CACHE
    aliases: dict[str, str] = {}
    try:
        import paramiko

        path = os.path.join(os.path.expanduser("~"), ".ssh", "config")
        if os.path.isfile(path):
            cfg = paramiko.SSHConfig()
            with open(path, encoding="utf-8") as fh:
                cfg.parse(fh)
            for host in cfg.get_hostnames():
                if host == "*" or host in aliases:
                    continue
                real = cfg.lookup(host).get("hostname")
                if real and real != host:
                    aliases[host] = real
    except Exception:  # noqa: BLE001 - 配置读不动不该让链接生成崩掉
        pass
    _SSH_ALIAS_CACHE = aliases
    return aliases


def _web_host(host: str) -> str:
    """SSH 主机 → 网页主机；认不出返回空串（宁可不给链接，也别给个死链）.

    GitHub 官方 443 通道 ssh.github.com 只跑 SSH，网页仍是 github.com。
    """
    host = (host or "").strip().lower()
    if not host:
        return ""
    real = _ssh_host_aliases().get(host) or host
    if real.startswith("ssh."):
        real = real[4:]
    # 换机器没带 ssh_config 时的兜底：github.com-xxx 是多账号别名的固定写法
    if real.startswith("github.com-"):
        real = "github.com"
    # 不像域名（连点都没有）又解析不出真实主机 → 不给链接
    if "." not in real:
        return ""
    return real


def repo_url(remote: str) -> str:
    """git remote 地址 → 能点开的网页地址；认不出就返回空串（宁可不给链接，也别给个死链）.

    三种常见写法都要认：
      git@github.com:user/repo.git        （SSH，最常见）
      ssh://git@host:2222/user/repo.git   （带端口/自定义 sshd）
      https://github.com/user/repo.git    （https，去掉 .git）
    自建 GitLab / Gitee / Coding 同理，不写死域名。
    SSH Host 别名（~/.ssh/config）先还原成真实 HostName 再拼链接。
    """
    r = (remote or "").strip()
    if not r:
        return ""

    # git@host:path  →  host/path
    m = re.match(r"^[\w.\-]+@([^:/]+):(.+)$", r)
    if m:
        host = _web_host(m.group(1))
        return _tidy_repo(f"https://{host}/{m.group(2)}") if host else ""

    # ssh://user@host:port/path
    m = re.match(r"^ssh://(?:[\w.\-]+@)?([^/:]+)(?::\d+)?/(.+)$", r)
    if m:
        host = _web_host(m.group(1))
        return _tidy_repo(f"https://{host}/{m.group(2)}") if host else ""

    if r.startswith(("http://", "https://")):
        return _tidy_repo(r)
    return ""


def _tidy_repo(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u


def summary_of(path: str) -> str:
    """README 第一段 → 列表页「这是什么」。取不到就空着，不编造."""
    readme = _find_readme(path)
    if not readme:
        return ""
    try:
        stamp = f"{readme}:{os.path.getmtime(readme)}"
    except OSError:
        stamp = readme
    cached = _summary_cache.get(stamp)
    if cached is not None:
        return cached

    text = read_text(readme, 16384)
    lines: list[str] = []
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if started:
                break
            continue
        if not started and (
            line.startswith("#") or line.startswith("![") or line.startswith("[![")
            or line.startswith("---") or line.startswith("<")
        ):
            continue
        started = True
        lines.append(line)

    joined = " ".join(lines)
    joined = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", joined)
    joined = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", joined)
    joined = re.sub(r"[`*_>#|]", "", joined)
    joined = re.sub(r"<[^>]+>", "", joined)
    joined = re.sub(r"\s+", " ", joined).strip()
    out = joined[:140]
    _summary_cache[stamp] = out
    return out


# ---------------------------------------------------------------- 截图探测
PREVIEW_NAMES = ("preview.png", "screenshot.png", "cover.png", "preview.jpg", "screenshot.jpg")
PREVIEW_DIRS = ("docs/assets/screenshots/current", "docs/assets/screenshots", "screenshots", "docs", "images", "assets")


def find_preview(path: str) -> str | None:
    for name in PREVIEW_NAMES:
        if os.path.isfile(os.path.join(path, name)):
            return name
    for sub in PREVIEW_DIRS:
        d = os.path.join(path, *sub.split("/"))
        if not os.path.isdir(d):
            continue
        try:
            files = sorted(os.listdir(d))
        except OSError:
            continue
        for f in files:
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                return f"{sub}/{f}"
    return None


# ------------------------------------------------------------------ 扫描
def root_score(names: set[str]) -> tuple[int, list[str]]:
    """给一个目录打分，判断它像不像「项目根」；返回 (分数, 命中的信号说明).

    打分而不是一刀切，是因为项目根没有唯一标志。GitHub 上真实的仓库根通常长这样：
        .git  README.md  LICENSE  .gitignore  package.json  .github/workflows/
    有几项算几项 —— 攒够 5 分才当作项目，只有 README（3 分）或只有清单（4 分）
    都还差一口气，进候选让人看一眼。
    """
    score = 0
    hits: list[str] = []
    for markers, weight, label in ROOT_SIGNALS:
        if markers & names:
            score += weight
            hits.append(f"{label} +{weight}")

    # 只有文档没有代码的目录多半是 docs/，不可能是项目根
    code = sum(1 for n in names if n.endswith(CODE_EXTS))
    if code >= 3:
        score += 1
        hits.append(f"根目录有 {code} 个源码文件 +1")

    return score, hits


def classify_dir(path: str, names: set[str]) -> str:
    """三档判定：auto（自动收编）/ candidate（候选，等人定）/ ""（不是项目，不收）.

    先做减法（参考 GitHub Linguist 的 vendored/generated 思路），再按特征打分：

    1. `_` / `.` 开头   —— 用户自己的临时 / 备份 / 隐藏目录约定，直接跳过
    2. 浏览器/应用数据  —— Chrome profile 里也有 README，靠特征文件认出来
    3. 打分 ≥5         —— auto。真实项目根通常同时有 .git / 清单 / README / LICENSE
    4. 打分 3–4        —— candidate。像项目但证据不够硬，交给人工定
    5. 打分 <3         —— 跳过。光有一堆源码文件不等于项目
    """
    base = os.path.basename(path.rstrip("\\/"))

    if base.startswith(("_", ".")):
        return ""

    # 浏览器 / 应用数据目录里也有 README，别被骗
    if looks_like_app_data(names):
        return ""

    score, _hits = root_score(names)
    if score >= SCORE_AUTO:
        return "auto"
    if score >= SCORE_CANDIDATE:
        return "candidate"
    return ""


def is_project_dir(path: str, names: set[str]) -> bool:
    """兼容旧调用：只要不是「明确不是项目」就算。—— 判定请用 classify_dir."""
    return classify_dir(path, names) != ""


def scan_project(path: str, rules: dict | None = None, state: str = "auto") -> ProjectInfo:
    name, src, conf = resolve_name(path)
    is_git, last_dt, dirty_n, unpushed_n, remote = git_info(path)

    if last_dt is None:
        # 非 git 项目退化用目录 mtime，避免活跃度一片空白
        try:
            last_dt = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            last_dt = None

    return ProjectInfo(
        path=path,
        dirname=os.path.basename(path.rstrip("\\/")),
        name=name,
        name_src=src,
        name_conf=conf,
        state=state,
        stack=detect_stack(path),
        active=active_level(last_dt, rules),
        is_git=is_git,
        last_commit=last_dt.strftime("%Y-%m-%d %H:%M:%S") if last_dt else None,
        dirty=dirty_n > 0,
        dirty_n=dirty_n,
        unpushed_n=unpushed_n,
        remote=remote[:200],
        summary=summary_of(path),
        preview=find_preview(path),
    )


def discover_paths(roots: list[str], max_depth: int = 3, with_state: bool = False):
    """只做目录发现（纯 os 调用，很快）；命中项目就停止下探.

    with_state=True 时返回 [(path, state)]，state 为 auto / candidate。
    默认返回 [path]，保持旧调用方不变。
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            try:
                names = set(os.listdir(cur))
            except OSError:
                continue

            # 用户可能直接把"某个项目文件夹"加成根目录 —— 根自己就是项目时要认
            if depth == 0 and any(m in names for m in PROJECT_MARKERS):
                if cur not in seen:
                    seen.add(cur)
                    found.append((cur, "auto"))
                continue

            if depth > 0:
                # 浏览器 / 应用数据目录：不记、也不往下翻（里面几千个缓存文件）
                if looks_like_app_data(names):
                    continue

                state = classify_dir(cur, names)
                if state:
                    if cur not in seen:
                        seen.add(cur)
                        found.append((cur, state))
                    continue  # 命中就停止下探，别钻进 node_modules

                # 自己不是项目，而且名字是容器名（src / tools / app …）——
                # 里面装的是零件，再往下翻只会把 xhs_lib、components 当项目捞上来
                if os.path.basename(cur).lower() in CONTAINER_NAMES:
                    continue

            if depth >= max_depth:
                continue

            for n in names:
                if n.startswith(".") or n in EXCLUDE_DIRS:
                    continue
                child = os.path.join(cur, n)
                if os.path.isdir(child):
                    stack.append((child, depth + 1))

    return found if with_state else [p for p, _ in found]


def scan_all(
    roots: list[str],
    max_depth: int = 3,
    rules: dict | None = None,
    workers: int = 8,
) -> list[ProjectInfo]:
    """扫多个根目录，返回项目列表（含候选，state 区分）。

    两段式：先串行发现路径（纯文件判断），再并发做 git 探测
    —— git 子进程在 Windows 上启动就要几十毫秒，串行 50 个必然超 5s。
    """
    pairs = discover_paths(roots, max_depth, with_state=True)
    if not pairs:
        return []

    with ThreadPoolExecutor(max_workers=min(workers, len(pairs))) as pool:
        infos = list(pool.map(lambda ps: scan_project(ps[0], rules, ps[1]), pairs))

    return sorted(infos, key=lambda p: p.path.lower())


def sync(roots: list[str], max_depth: int = 3, rules: dict | None = None, workers: int = 8) -> dict:
    """扫描并落库（两段式：事务外扫描，事务内只写）.

    返回 {"scanned": n, "at": 时间}。扫描失败不抛，交给调用方决定怎么提示。
    """
    from . import db

    infos = scan_all(roots, max_depth=max_depth, rules=rules, workers=workers)
    ts = db.now()
    if not infos:
        return {"scanned": 0, "at": ts}

    with db.tx() as c:
        for p in infos:
            c.execute(
                """
                INSERT INTO project(path, dirname, name_auto, name_src, name_conf, stack,
                                    active, is_git, last_commit, dirty,
                                    dirty_n, unpushed_n, remote, summary, preview,
                                    state, first_seen, last_seen)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    dirname     = excluded.dirname,
                    name_auto   = excluded.name_auto,
                    name_src    = excluded.name_src,
                    name_conf   = excluded.name_conf,
                    stack       = excluded.stack,
                    active      = excluded.active,
                    is_git      = excluded.is_git,
                    last_commit = excluded.last_commit,
                    dirty       = excluded.dirty,
                    dirty_n     = excluded.dirty_n,
                    unpushed_n  = excluded.unpushed_n,
                    remote      = excluded.remote,
                    summary     = excluded.summary,
                    preview     = COALESCE(excluded.preview, project.preview),
                    state       = CASE WHEN project.state_locked=1
                                       THEN project.state ELSE excluded.state END,
                    last_seen   = excluded.last_seen
                """,
                (
                    p.path, p.dirname, p.name, p.name_src, p.name_conf, p.stack,
                    p.active, int(p.is_git), p.last_commit, int(p.dirty),
                    p.dirty_n, p.unpushed_n, p.remote, p.summary, p.preview,
                    p.state, ts, ts,
                ),
            )

        # 引擎这次减掉的旧条目 → 标 dropped（不删数据，改名/备注都还在，随时能恢复）。
        # 只处理本次扫描根目录子树内的，别误伤别的根；人工认可过的（改过名或置顶）不动。
        kept = {p.path for p in infos}
        roots_abs = [os.path.abspath(r).rstrip("\\/") for r in roots]
        stale = [
            r["path"]
            for r in c.execute("SELECT path, state, user_name, user_pinned FROM project").fetchall()
            if r["path"] not in kept
            and r["state"] != "dropped"
            and not r["user_name"]
            and not r["user_pinned"]
            and any(r["path"] == ra or r["path"].startswith(ra + os.sep) for ra in roots_abs)
        ]
        if stale:
            c.executemany("UPDATE project SET state='dropped' WHERE path=?", [(p,) for p in stale])

    return {"scanned": len(infos), "at": ts}


_UPSERT_SQL = """
INSERT INTO project(path, dirname, name_auto, name_src, name_conf, stack,
                    active, is_git, last_commit, dirty,
                    dirty_n, unpushed_n, remote, summary, preview,
                    state, first_seen, last_seen)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(path) DO UPDATE SET
    dirname     = excluded.dirname,
    name_auto   = excluded.name_auto,
    name_src    = excluded.name_src,
    name_conf   = excluded.name_conf,
    stack       = excluded.stack,
    active      = excluded.active,
    is_git      = excluded.is_git,
    last_commit = excluded.last_commit,
    dirty       = excluded.dirty,
    dirty_n     = excluded.dirty_n,
    unpushed_n  = excluded.unpushed_n,
    remote      = excluded.remote,
    summary     = excluded.summary,
    preview     = COALESCE(excluded.preview, project.preview),
    state       = CASE WHEN project.state_locked=1
                       THEN project.state ELSE excluded.state END,
    last_seen   = excluded.last_seen
"""


def sync_one(path: str, rules: dict | None = None) -> dict:
    """只重扫一个项目，落库后返回 {"path","name","at"}.

    为什么需要它：全量扫描要遍历所有根目录，改完一个项目的 README 或提交代码后
    想看到新结果，不该等整盘扫完 —— 一个项目的刷新就该是毫秒级。

    行为跟全量扫描一致（同一段 UPSERT），所以不会出现"单独刷"和"刷全部"结果不同。
    """
    from . import db

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(path)

    info = scan_project(path, rules)
    ts = db.now()
    row = db.query_one("SELECT state, state_locked FROM project WHERE path=?", (path,))
    if row is not None and row["state_locked"]:
        info.state = row["state"]  # 人拍过板的判定，重扫不许覆盖

    with db.tx() as c:
        c.execute(
            _UPSERT_SQL,
            (
                info.path, info.dirname, info.name, info.name_src, info.name_conf, info.stack,
                info.active, int(info.is_git), info.last_commit, int(info.dirty),
                info.dirty_n, info.unpushed_n, info.remote, info.summary, info.preview,
                info.state, ts, ts,
            ),
        )
    return {"path": info.path, "name": info.name, "at": ts}
