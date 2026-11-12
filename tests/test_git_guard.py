"""门禁脚本的单测。

只测纯逻辑（规则表、URL 归一化、提交信息校验）——不 spawn 真的 git 进程。
hook 在真实 push / commit 场景下的行为由 .githooks + publish_public.py 的
端到端流程保证，那部分在 docs/GIT_FLOW.md 里有实测记录。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "git_guard_mod", ROOT / "scripts" / "git_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


g = _load()


# ---------------------------------------------------------------- URL 归一化


def test_guard_normalize_url_treats_git_suffix_and_slash_as_same():
    a = g.normalize_url("https://github.com/owner/repo.git")
    b = g.normalize_url("https://github.com/owner/repo/")
    c = g.normalize_url("HTTPS://GitHub.com/owner/repo")
    assert a == b == c, (a, b, c)


# ---------------------------------------------------------------- 提交信息


def _write_msg(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_guard_commit_msg_rejects_vague_subject():
    for bad in ("wip", "update", "修改", "fix", "tmp"):
        p = _write_msg(bad + "\n")
        try:
            assert g.run_commit_msg(p) == 1, f"应当拒绝：{bad}"
        finally:
            os.remove(p)


def test_guard_commit_msg_accepts_conventional_subject():
    p = _write_msg("fix(readme): 本地相对图片 404，改走资源路由\n\n补充说明\n")
    try:
        assert g.run_commit_msg(p) == 0
    finally:
        os.remove(p)


def test_guard_commit_msg_ignores_comment_only_message():
    """merge / revert 自动生成的信息只有注释，不该误拦."""
    p = _write_msg("# Please enter the commit message\n#\n")
    try:
        assert g.run_commit_msg(p) == 0
    finally:
        os.remove(p)


# ---------------------------------------------------------------- 密钥规则


def _secret_hit(text: str) -> bool:
    return any(pat.search(text) for pat, _ in g.SECRET_PATTERNS)


def test_guard_secret_rules_catch_real_token_shapes():
    # 运行时拼接：源码里不留连续密钥串，否则会被自己的 pre-commit 拦下
    fake_sk = "sk-" + "proj-" + "a" * 24
    assert _secret_hit('KEY = "%s"' % fake_sk)
    assert _secret_hit("token: " + "ghp_" + "a" * 36)
    assert _secret_hit("AKIA" + "B" * 16)


def test_guard_secret_rules_do_not_flag_split_test_fixture():
    """测试里把私钥串拆成两段拼接，正是为了不被扫描器误报."""
    line = 'body = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\\nabc\\n"'
    assert not _secret_hit(line), "拆开写的测试 fixture 不该被当成真私钥"


def test_guard_secret_rules_ignore_short_dummy_password():
    assert not _secret_hit('password = "123456"')
    assert not _secret_hit('token = "sk-live-9f8e7d6c5b4a"')


# ---------------------------------------------------------------- 文件名规则


def _bad_name(rel: str) -> bool:
    return any(pat.search(rel) for pat, _ in g.BAD_NAME_GLOBS)


def test_guard_bad_names_block_data_files():
    assert _bad_name("data/workbench.db")
    assert _bad_name("cf_1h_demo.json")
    assert _bad_name("secrets/id_rsa")
    assert _bad_name("release/app.zip")


def test_guard_bad_names_allow_normal_source():
    for ok in ("wb/scan.py", "templates/base.html", "static/app.css",
               "tests/test_scan.py", "docs/GIT_FLOW.md"):
        assert not _bad_name(ok), ok


# ---------------------------------------------------------------- 隐私清单


def test_guard_privacy_terms_come_from_config_not_source():
    """隐私词必须由 .wb-publish.json 提供，不能写死在脚本里.

    原因：git_guard.py 是要对外发布的，规则表里列着什么词，
    等于公开「哪些是私有项目」。
    """
    src = (ROOT / "scripts" / "git_guard.py").read_text(encoding="utf-8")
    conf_path = ROOT / ".wb-publish.json"
    if not conf_path.exists():
        return  # 公开克隆里没有这份配置
    import json
    terms = json.loads(conf_path.read_text(encoding="utf-8")).get("must_not_contain", [])
    for t in terms:
        assert t not in src, f"隐私词「{t}」不该出现在可发布的门禁脚本里"
