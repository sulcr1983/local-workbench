"""T4 扫描与命名解析验收."""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta

from wb import scan


def _mk(files: dict[str, str] | None = None, name: str | None = None) -> str:
    """造一个临时目录，返回路径；给 name 时在临时目录下再建一层同名目录."""
    root = tempfile.mkdtemp(prefix="wb-scan-")
    d = os.path.join(root, name) if name else root
    os.makedirs(d, exist_ok=True)
    for rel, content in (files or {}).items():
        p = os.path.join(d, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    return d


# ---------------------------------------------------------------- 命名解析
def test_name_from_readme_h1():
    d = _mk({"README.md": "# 苏哥的钓鱼竿\n\n一个钓鱼记录小工具。\n"})
    name, src, conf = scan.resolve_name(d)
    assert name == "苏哥的钓鱼竿", name
    assert src == "readme_h1" and conf == 0.90


def test_name_from_readme_h1_skips_badges():
    d = _mk({"README.md": "![badge](x.svg)\n\n#   AcmeAuto Insights  \n"})
    name, src, conf = scan.resolve_name(d)
    assert name == "AcmeAuto Insights"
    assert conf == 0.90


def test_name_from_manifest_when_no_h1():
    d = _mk({"package.json": '{"name": "acme-auto", "version": "1.0.0"}', "README.md": "没有标题的说明"})
    name, src, conf = scan.resolve_name(d)
    assert (name, src, conf) == ("acme-auto", "manifest", 0.72)


def test_name_from_pyproject():
    d = _mk({"pyproject.toml": '[project]\nname = "demo-collector"\nversion = "0.1.0"\n'})
    name, src, conf = scan.resolve_name(d)
    assert (name, conf) == ("demo-collector", 0.72)


def test_name_from_dirname_lowercase():
    base = _mk({}, name="my-cool_tool")
    name, src, conf = scan.resolve_name(base)
    assert name == "My Cool Tool", name
    assert src == "dirname" and conf == 0.45


def test_name_from_dirname_chinese_kept():
    base = _mk({}, name="一键开门")
    name, _, conf = scan.resolve_name(base)
    assert name == "一键开门"
    assert conf == 0.45


# ---------------------------------------------------------------- 排除规则
def test_excluded_dirs_are_not_projects():
    root = _mk()
    for sub in ("node_modules", ".venv", "__pycache__", "dist"):
        p = os.path.join(root, sub)
        os.makedirs(p)
        with open(os.path.join(p, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"name":"x"}')
    real = os.path.join(root, "real-project")
    os.makedirs(real)
    with open(os.path.join(real, "requirements.txt"), "w", encoding="utf-8") as f:
        f.write("flask\n")

    paths = scan.discover_paths([root])
    names = [os.path.basename(p) for p in paths]
    assert names == ["real-project"], names


def test_scan_stops_inside_project():
    """项目内部不再下探 —— 否则一个前端工程能刷出几十个假项目."""
    root = _mk()
    proj = os.path.join(root, "app")
    os.makedirs(os.path.join(proj, "src", "components"))
    with open(os.path.join(proj, "package.json"), "w", encoding="utf-8") as f:
        f.write('{"name":"app"}')
    with open(os.path.join(proj, "src", "components", "requirements.txt"), "w", encoding="utf-8") as f:
        f.write("x\n")

    paths = scan.discover_paths([root])
    assert paths == [proj], paths


def test_hidden_dirs_skipped():
    root = _mk()
    hidden = os.path.join(root, ".cache-thing")
    os.makedirs(hidden)
    with open(os.path.join(hidden, "requirements.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    assert scan.discover_paths([root]) == []


# ---------------------------------------------------------------- 活跃度
def test_active_level_bands():
    now = datetime.now()
    assert scan.active_level(now - timedelta(days=3)) == "活跃"
    assert scan.active_level(now - timedelta(days=7)) == "活跃"
    assert scan.active_level(now - timedelta(days=8)) == "放缓"
    assert scan.active_level(now - timedelta(days=30)) == "放缓"
    assert scan.active_level(now - timedelta(days=31)) == "停滞"
    assert scan.active_level(now - timedelta(days=90)) == "停滞"
    assert scan.active_level(now - timedelta(days=91)) == "可能废弃"
    assert scan.active_level(None) == "未知"


def test_active_level_custom_rules():
    now = datetime.now()
    rules = {"active": 1, "slow": 2, "stale": 3}
    assert scan.active_level(now - timedelta(days=2), rules) == "放缓"
    assert scan.active_level(now - timedelta(days=5), rules) == "可能废弃"


# ---------------------------------------------------------------- 技术栈
def test_detect_stack():
    assert scan.detect_stack(_mk({"requirements.txt": "flask\n"})) == "Python"
    assert scan.detect_stack(_mk({"package.json": "{}"})) == "Node"
    assert scan.detect_stack(_mk({"go.mod": "module x\n"})) == "Go"
    assert scan.detect_stack(_mk({"Cargo.toml": "[package]\n"})) == "Rust"
    assert scan.detect_stack(_mk({"index.html": "<html></html>"})) == "静态页"
    assert scan.detect_stack(_mk({"notes.txt": "x"})) == "未识别"


# ---------------------------------------------------------------- 截图
def test_find_preview():
    d = _mk({"preview.png": "x"})
    assert scan.find_preview(d) == "preview.png"
    d2 = _mk({"docs/assets/screenshots/current/a.png": "x"})
    assert scan.find_preview(d2) == "docs/assets/screenshots/current/a.png"
    assert scan.find_preview(_mk({})) is None


# ---------------------------------------------------------------- 性能
def test_scan_50_dirs_under_5s():
    root = _mk()
    for i in range(50):
        p = os.path.join(root, f"proj-{i:02d}")
        os.makedirs(p)
        with open(os.path.join(p, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("flask\n")
        with open(os.path.join(p, "README.md"), "w", encoding="utf-8") as f:
            f.write(f"# 项目 {i}\n")

    t0 = time.perf_counter()
    projects = scan.scan_all([root])
    cost = time.perf_counter() - t0

    assert len(projects) == 50, f"应识别 50 个，实际 {len(projects)}"
    assert cost <= 5.0, f"扫描 50 个目录耗时 {cost:.2f}s，超过 5s"
    assert all(p.name.startswith("项目") for p in projects)


def test_scan_all_returns_sorted_unique():
    root = _mk()
    for n in ("b-proj", "a-proj", "c-proj"):
        d = os.path.join(root, n)
        os.makedirs(d)
        with open(os.path.join(d, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("x")
    names = [os.path.basename(p.path) for p in scan.scan_all([root])]
    assert names == ["a-proj", "b-proj", "c-proj"]


def test_scan_missing_root_is_ignored():
    """路径不存在不该崩，返回空列表."""
    assert scan.scan_all([r"Z:\definitely-not-here"]) == []


# ---------------------------------------------------------------- 三档判定
# 口径（人拍板）：没有 README 的不算项目。这组用例把这条锁死。
def test_no_readme_not_a_project():
    """十个源码文件也算不上项目 —— 没有 README 就是没有."""
    root = _mk()
    d = os.path.join(root, "some-scripts")
    os.makedirs(d)
    for i in range(10):
        with open(os.path.join(d, f"s{i}.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
    assert scan.classify_dir(d, set(os.listdir(d))) == ""


def test_readme_alone_is_only_a_candidate():
    """README 3 分，够不上 5 分的自动收编线 —— 只有说明文档的目录可能就是个 docs/."""
    d = _mk({"README.md": "# 某项目\n\n说明。\n"})
    assert scan.classify_dir(d, set(os.listdir(d))) == "candidate"


def test_readme_plus_gitignore_is_a_project():
    """README(3) + .gitignore(2) = 5 分 —— 这就是常见的小项目根."""
    d = _mk({"README.md": "# 某项目\n", ".gitignore": "*.pyc\n"})
    assert scan.classify_dir(d, set(os.listdir(d))) == "auto"


def test_manifest_plus_readme_is_a_project():
    """清单(4) + README(3) = 7 分。"""
    d = _mk({"README.md": "# x\n", "pyproject.toml": '[project]\nname="x"\n'})
    assert scan.classify_dir(d, set(os.listdir(d))) == "auto"


def test_full_github_style_root_scores_high():
    """.git + README + LICENSE + .gitignore + package.json + .github/ —— GitHub 上的标准仓库根."""
    d = _mk({
        "README.md": "# x\n", "LICENSE": "MIT", ".gitignore": "node_modules\n",
        "package.json": '{"name":"x"}', ".github/workflows/ci.yml": "on: push\n",
    })
    os.makedirs(os.path.join(d, ".git"), exist_ok=True)
    score, hits = scan.root_score(set(os.listdir(d)))
    assert score >= 15, (score, hits)
    assert scan.classify_dir(d, set(os.listdir(d))) == "auto"


def test_manifest_without_readme_is_candidate():
    """只有清单 4 分 —— 是工程但不完整，进候选."""
    d = _mk({"package.json": '{"name":"x"}'})
    assert scan.classify_dir(d, set(os.listdir(d))) == "candidate"


def test_git_alone_is_enough():
    """.git 5 分，有它就是仓库."""
    d = _mk({})
    os.makedirs(os.path.join(d, ".git"), exist_ok=True)
    assert scan.classify_dir(d, set(os.listdir(d))) == "auto"


def test_underscore_prefix_skipped_even_with_readme():
    """_ 开头是临时/备份目录的自己约定，有 README 也不算项目."""
    d = _mk({"README.md": "# 备份\n"}, name="_backup-x")
    assert scan.classify_dir(d, set(os.listdir(d))) == ""


def test_container_dir_not_deep_scanned():
    """tools 里的模块不该被当成独立项目 —— 这是之前污染的主因."""
    root = _mk()
    inner = os.path.join(root, "tools", "xhs_lib")
    os.makedirs(inner)
    with open(os.path.join(inner, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 内部库\n")
    assert scan.discover_paths([root]) == []


def test_app_dir_with_enough_signals_is_project():
    """容器名不影响判定 —— 叫 app 的目录攒够分就是项目."""
    root = _mk()
    app = os.path.join(root, "app")
    os.makedirs(app)
    for name, body in (("README.md", "# 主应用\n"), ("package.json", '{"name":"app"}'),
                       (".gitignore", "node_modules\n")):
        with open(os.path.join(app, name), "w", encoding="utf-8") as f:
            f.write(body)
    assert scan.discover_paths([root]) == [app]


def test_candidate_state_survives_scan_all():
    root = _mk()
    d = os.path.join(root, "legacy-repo")
    os.makedirs(d)
    with open(os.path.join(d, "requirements.txt"), "w", encoding="utf-8") as f:
        f.write("flask\n")
    infos = scan.scan_all([root])
    got = {os.path.basename(i.path): i.state for i in infos}
    assert got == {"legacy-repo": "candidate"}, got


def test_browser_profile_with_readme_skipped():
    """Chrome 的 profile 里自带一个 README —— 别把它当成项目."""
    root = _mk()
    prof = os.path.join(root, "cf-browser", "Default")
    os.makedirs(prof)
    for name in ("Preferences", "Bookmarks", "History", "Web Data", "Cookies"):
        with open(os.path.join(prof, name), "w", encoding="utf-8") as f:
            f.write("{}")
    with open(os.path.join(prof, "README"), "w", encoding="utf-8") as f:
        f.write("Google Chrome settings and storage represent user-selected preferences")
    assert scan.classify_dir(prof, set(os.listdir(prof))) == ""
    assert scan.discover_paths([root]) == [], "浏览器 profile 不该出现在项目里"


# ---------------------------------------------------------------- 仓库链接
def test_repo_url_understands_ssh_scp_form():
    from wb.scan import repo_url

    assert repo_url("git@github.com:su/repo.git") == "https://github.com/su/repo"
    assert repo_url("git@gitee.com:su/repo") == "https://gitee.com/su/repo"


def test_repo_url_understands_ssh_url_with_port():
    from wb.scan import repo_url

    assert repo_url("ssh://git@gitlab.example.cn:2222/grp/repo.git") == "https://gitlab.example.cn/grp/repo"


def test_repo_url_understands_https_and_strips_git():
    from wb.scan import repo_url

    assert repo_url("https://github.com/su/repo.git") == "https://github.com/su/repo"
    assert repo_url("https://github.com/su/repo/") == "https://github.com/su/repo"


def test_repo_url_gives_up_on_local_paths():
    from wb.scan import repo_url

    assert repo_url("D:/local/repo") == ""
    assert repo_url("") == ""


def test_repo_site_labels_known_hosts():
    from wb.blueprints.projects import repo_site

    assert repo_site("https://github.com/a/b") == "GitHub"
    assert repo_site("https://gitee.com/a/b") == "Gitee"
    assert repo_site("https://gitlab.company.cn/a/b") == "GitLab"
    assert repo_site("https://git.company.cn/a/b") == "代码仓库"


def test_repo_url_resolves_ssh_config_alias():
    """多账号 SSH 别名要还原成真实 HostName，不能拿别名当域名拼死链."""
    import wb.scan as scan

    old = scan._SSH_ALIAS_CACHE
    scan._SSH_ALIAS_CACHE = {"gh-work": "github.com"}
    try:
        assert scan.repo_url("git@gh-work:su/repo.git") == "https://github.com/su/repo"
    finally:
        scan._SSH_ALIAS_CACHE = old


def test_repo_url_alias_fallback_without_ssh_config():
    """换机器没带 ssh_config，github.com-xxx 兜底也不能出死链."""
    import wb.scan as scan

    old = scan._SSH_ALIAS_CACHE
    scan._SSH_ALIAS_CACHE = {}
    try:
        assert scan.repo_url("git@github.com-myalias:su/repo.git") == "https://github.com/su/repo"
    finally:
        scan._SSH_ALIAS_CACHE = old


def test_repo_url_normalizes_github_443_gateway():
    """GitHub 官方 443 通道 ssh.github.com 只跑 SSH，网页仍是 github.com."""
    import wb.scan as scan

    old = scan._SSH_ALIAS_CACHE
    scan._SSH_ALIAS_CACHE = {}
    try:
        assert scan.repo_url("ssh://git@ssh.github.com:443/su/repo.git") == "https://github.com/su/repo"
    finally:
        scan._SSH_ALIAS_CACHE = old


def test_repo_url_unknown_host_gives_up():
    """认不出的主机返回空串，不编造一个点不开的链接."""
    import wb.scan as scan

    old = scan._SSH_ALIAS_CACHE
    scan._SSH_ALIAS_CACHE = {}
    try:
        assert scan.repo_url("git@mybox:su/repo.git") == ""
    finally:
        scan._SSH_ALIAS_CACHE = old


# ---------------------------------------------------------------- 技术栈：散装项目
def test_detect_stack_falls_back_to_source_suffix():
    """没有清单文件的小项目（一堆散装 .py）不该显示「未识别」."""
    import tempfile

    from wb.scan import detect_stack

    d = tempfile.mkdtemp(prefix="wb-stack-")
    for f in ("a.py", "b.py", "c.py", "README.md"):
        open(os.path.join(d, f), "w").close()
    # 不再返回「（散装）」这种自造词 —— 那是给开发看的，用户看不懂
    assert detect_stack(d) == "Python", detect_stack(d)


def test_detect_stack_prefers_manifest_over_suffix():
    """有 package.json 就以它为准，别被散装文件带偏."""
    import tempfile

    from wb.scan import detect_stack

    d = tempfile.mkdtemp(prefix="wb-stack2-")
    open(os.path.join(d, "package.json"), "w").close()
    for f in ("a.py", "b.py", "c.py", "d.py"):
        open(os.path.join(d, f), "w").close()
    assert detect_stack(d) == "Node", detect_stack(d)


def test_detect_stack_returns_unknown_for_docs_only():
    """只有文档和图片的目录，老老实实说未识别，不硬塞一个语言."""
    import tempfile

    from wb.scan import detect_stack

    d = tempfile.mkdtemp(prefix="wb-stack3-")
    for f in ("README.md", "a.png", ".env"):
        open(os.path.join(d, f), "w").close()
    assert detect_stack(d) == "未识别", detect_stack(d)


def test_stack_key_strips_suffix():
    """「Python（散装）」要能匹配到 py 图标，不能掉到 none."""
    from wb.viz import stack_key

    assert stack_key("Python（散装）") == "py"
    assert stack_key("Node(散装)") == "node"
    assert stack_key("Python") == "py"
    assert stack_key("未识别") == "none"
    assert stack_key("") == "none"


# ---------------------------------------------------------------- 命名解析：README 标题
def test_readme_title_reads_html_h1():
    """居中排版常用 <h1>，只认 Markdown `#` 会把安装说明当项目名."""
    import tempfile

    from wb.scan import resolve_name

    d = tempfile.mkdtemp(prefix="wb-h1-")
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write(
            '<div align="center">\n'
            '  <img src="logo.png" alt="logo" />\n'
            "  <h1>示例英语 · 演示用英语学习应用</h1>\n"
            "  <p><strong>给备考 KET 的孩子做的刷题 PWA</strong></p>\n"
            "</div>\n\n---\n\n"
            "## 01 一眼看懂\n\n"
            "项目根目录起任意静态服务即可（零构建，不需要 npm install）\n"
        )
    name, src, _conf = resolve_name(d)
    assert name == "示例英语 · 演示用英语学习应用", name
    assert src == "readme_h1"


def test_readme_title_skips_install_instructions():
    """README 第一个 `#` 是安装说明时，不能拿它当名字."""
    import tempfile

    from wb.scan import resolve_name

    d = tempfile.mkdtemp(prefix="wb-h1b-")
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 我的项目\n\n## 快速开始\n\nnpm install 然后 npm run dev\n")
    name, _src, _conf = resolve_name(d)
    assert name == "我的项目", name


def test_readme_title_ignores_badges_and_links():
    import tempfile

    from wb.scan import resolve_name

    d = tempfile.mkdtemp(prefix="wb-h1c-")
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write("# [![build](a.svg)](b) 真实名字\n\n正文\n")
    name, _s, _c = resolve_name(d)
    assert name == "真实名字", name


def test_readme_with_only_install_line_falls_back_to_dirname():
    """整篇 README 没有像样的标题 → 退回目录名，不要硬编一个名字."""
    import tempfile

    from wb.scan import resolve_name

    d = tempfile.mkdtemp(prefix="wb-h1d-")
    os.makedirs(os.path.join(d, "my-proj"))
    with open(os.path.join(d, "my-proj", "README.md"), "w", encoding="utf-8") as f:
        f.write("# 安装方法\n\npip install -r requirements.txt\n")
    name, src, _c = resolve_name(os.path.join(d, "my-proj"))
    assert src == "dirname", (name, src)
