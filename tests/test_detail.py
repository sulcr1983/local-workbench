"""T6 项目详情页验收：截图、README 净化与缓存、路径安全."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import time

from run_all import skip
from wb import create_app, db

# 1x1 透明 PNG
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _setup(files: dict[str, str] | None = None):
    root = tempfile.mkdtemp(prefix="wb-det-")
    proj = os.path.join(root, "demo")
    os.makedirs(proj, exist_ok=True)
    for rel, content in (files or {}).items():
        p = os.path.join(proj, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)

    ts = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO root(path, added_at) VALUES(?,?)", (root, ts))
        c.execute(
            """INSERT INTO project(path, dirname, name_auto, name_src, name_conf, stack,
                                   active, is_git, dirty, first_seen, last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (proj, "demo", "演示项目", "readme_h1", 0.9, "Python", "活跃", 0, 0, ts, ts),
        )
    return app.test_client(), proj, root


def _html(resp) -> str:
    return resp.get_data(as_text=True)


# ------------------------------------------------------------- 页面结构
def test_detail_page_renders():
    c, _, _ = _setup({"README.md": "# 演示项目\n\n正文。\n"})
    resp = c.get("/project/demo")
    assert resp.status_code == 200
    body = _html(resp)
    assert "演示项目" in body


def test_shot_section_comes_before_readme():
    """截图在 README 之前 —— 详情页的信息顺序是硬要求."""
    c, _, _ = _setup({"README.md": "# 演示项目\n\n正文。\n"})
    body = _html(c.get("/project/demo"))
    # hero 里也有「README 标题」字样，所以拿卡片标题锚点比，别拿裸词
    assert body.index("首页截图") < body.index("美化阅读")


def test_empty_shot_is_a_single_line():
    c, _, _ = _setup({"README.md": "# 演示项目\n"})
    body = _html(c.get("/project/demo"))
    assert "还没有截图" in body
    assert "<img" not in body.split("首页截图")[1].split("README")[0], "没图时不该塞 img"


def test_has_preview_shows_img():
    c, proj, _ = _setup({"README.md": "# 演示项目\n"})
    with open(os.path.join(proj, "preview.png"), "wb") as f:
        f.write(base64.b64decode(PNG_B64))
    db.execute("UPDATE project SET preview='preview.png' WHERE path=?", (proj,))

    body = _html(c.get("/project/demo"))
    assert "/preview/demo" in body
    assert "更换" in body and "删除" in body


def test_preview_route_serves_file():
    c, proj, _ = _setup({"README.md": "# x\n"})
    with open(os.path.join(proj, "preview.png"), "wb") as f:
        f.write(base64.b64decode(PNG_B64))
    db.execute("UPDATE project SET preview='preview.png' WHERE path=?", (proj,))
    resp = c.get("/preview/demo")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"


# --------------------------------------------------------- README 安全
def test_readme_strips_script_and_javascript_url():
    c, _, _ = _setup({
        "README.md": "# 演示\n\n<script>alert(1)</script>\n\n[点我](javascript:alert(1))\n\n<img src=x onerror=alert(1)>\n"
    })
    body = _html(c.get("/project/demo"))
    # 页面自身有 <script src="/static/app.js">，所以只断言 README 里的攻击载荷没活下来
    assert "<script>alert" not in body.lower(), "script 标签没被清掉"
    assert "javascript:" not in body.lower(), "javascript: 协议没被清掉"
    assert "onerror" not in body.lower(), "事件属性没被清掉"


def test_readme_keeps_normal_markup():
    c, _, _ = _setup({"README.md": "# 标题\n\n**粗体** 和 [链接](https://example.com)\n\n- 一\n- 二\n"})
    body = _html(c.get("/project/demo"))
    assert "<strong>" in body or "<b>" in body
    assert 'href="https://example.com"' in body
    assert "<li>" in body


def test_readme_absent_hides_card():
    c, _, _ = _setup({"notes.txt": "没有 readme"})
    body = _html(c.get("/project/demo"))
    assert "已按白名单净化" not in body, "没有 README 时不该出现空卡片"


# ------------------------------------- README 本地相对资源改写（渲染口径 v2）
def test_readme_local_image_is_rewritten():
    """Markdown 图片与原生 HTML 图片都要改写，中文文件名要 URL 编码."""
    c, _, _ = _setup({
        "README.md": '# 演示\n\n![Logo](assets/logo.png)\n\n<img src="shots/中文 图.png" alt="图">\n'
    })
    body = _html(c.get("/project/demo"))
    assert "/readme-asset/demo?f=assets/logo.png" in body
    assert "f=shots/%E4%B8%AD%E6%96%87%20%E5%9B%BE.png" in body
    assert '<img src="assets/logo.png"' not in body


def test_readme_relative_link_is_rewritten_and_fragment_kept():
    c, _, _ = _setup({
        "README.md": "# 演示\n\n[变更日志](docs/变更日志.md#v105) · [接手](handover.md)\n"
    })
    body = _html(c.get("/project/demo"))
    assert "f=docs/%E5%8F%98%E6%9B%B4%E6%97%A5%E5%BF%97.md#v105" in body
    assert "f=handover.md" in body
    assert 'href="docs/变更日志.md#v105"' not in body


def test_readme_external_and_anchor_urls_untouched():
    c, _, _ = _setup({
        "README.md": "# 演示\n\n"
                     "[站点](https://example.com/a) · [邮件](mailto:x@y.com) · [锚点](#section)\n\n"
                     "![badge](https://img.shields.io/b.svg)\n"
    })
    body = _html(c.get("/project/demo"))
    assert 'href="https://example.com/a"' in body
    assert 'href="mailto:x@y.com"' in body
    assert 'href="#section"' in body
    assert 'src="https://img.shields.io/b.svg"' in body
    assert "/readme-asset/demo" not in body


def test_readme_traversal_url_not_rewritten():
    """.. 不能改写到资源路由（路由本身也会拦），保持原样."""
    c, _, _ = _setup({"README.md": "# 演示\n\n![x](../secret.png)\n"})
    body = _html(c.get("/project/demo"))
    assert "/readme-asset/" not in body
    assert 'src="../secret.png"' in body


def test_render_markdown_without_asset_base_keeps_urls():
    """纯函数场景不改写，调用方自己决定要不要传 rel."""
    from wb import security

    out = security.render_markdown("![x](assets/a.png)\n\n[doc](docs/a.md)\n")
    assert 'src="assets/a.png"' in out
    assert 'href="docs/a.md"' in out
    assert "/readme-asset/" not in out


def test_readme_image_url_end_to_end():
    """不是只验字符串：拿页面里真实的 src 去 GET，必须取到图."""
    c, proj, _ = _setup({"README.md": "# 演示\n\n![Logo](assets/logo.png)\n"})
    os.makedirs(os.path.join(proj, "assets"), exist_ok=True)
    with open(os.path.join(proj, "assets", "logo.png"), "wb") as f:
        f.write(base64.b64decode(PNG_B64))
    body = _html(c.get("/project/demo"))
    m = re.search(r'src="(/readme-asset/[^"]+)"', body)
    assert m, "页面里没找到改写后的图片 URL"
    resp = c.get(m.group(1).replace("&amp;", "&"))
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"


def test_old_format_hash_forces_rerender():
    """升级渲染口径后，库里 v1 格式的 hash 不能让页面静默显示旧 HTML."""
    raw = "# 演示\n\n![Logo](assets/logo.png)\n"
    c, proj, _ = _setup({"README.md": raw})
    old_hash = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    db.execute(
        "UPDATE project SET readme=?, readme_hash=? WHERE path=?",
        ("<h1>演示</h1><p>旧缓存</p>", old_hash, proj),
    )
    body = _html(c.get("/project/demo"))
    assert "/readme-asset/demo?f=assets/logo.png" in body
    assert "旧缓存" not in body
    assert db.scalar("SELECT readme_hash FROM project WHERE path=?", (proj,)) != old_hash


# ------------------------------------------- 居中排版 / 图片尺寸（渲染口径 v3）
def test_readme_keeps_center_align_and_img_width():
    """<div align="center"> 与 img[width] 要留住，且不影响相对图片改写."""
    raw = ('<div align="center">\n'
           '  <img src="assets/logo.png" width="128" alt="Logo">\n'
           '</div>\n\n'
           '<p align="right">右对齐</p>\n')
    c, proj, _ = _setup({"README.md": raw})
    c.get("/project/demo")
    html = db.scalar("SELECT readme FROM project WHERE path=?", (proj,))
    assert '<div align="center">' in html
    assert 'width="128"' in html
    assert '<p align="right">' in html
    assert "/readme-asset/demo?f=assets/logo.png" in html


def test_readme_style_and_event_attrs_still_stripped():
    """只放行纯展示属性：style / class / on* 一个都不能活下来."""
    raw = ('<div style="color:red" onclick="alert(1)" align="center">\n'
           '  <img src="assets/logo.png" width="128" onerror="alert(1)">\n'
           '</div>\n')
    c, proj, _ = _setup({"README.md": raw})
    c.get("/project/demo")
    html = db.scalar("SELECT readme FROM project WHERE path=?", (proj,))
    assert '<div align="center">' in html
    assert 'width="128"' in html
    assert "style=" not in html
    assert "onclick" not in html and "onerror" not in html
    assert "javascript" not in html.lower()


def test_v2_cache_is_invalidated_by_v3_renderer():
    """白名单改了但 hash 没变 = 静默失效；v3 必须让 v2 缓存自动重渲."""
    raw = '<div align="center">\n<img src="assets/logo.png" width="128" alt="Logo">\n</div>\n'
    c, proj, _ = _setup({"README.md": raw})
    old_hash = hashlib.sha1(("v2|" + raw).encode("utf-8")).hexdigest()
    db.execute(
        "UPDATE project SET readme=?, readme_hash=? WHERE path=?",
        ("<p>旧缓存</p>", old_hash, proj),
    )
    c.get("/project/demo")
    html = db.scalar("SELECT readme FROM project WHERE path=?", (proj,))
    assert '<div align="center">' in html
    assert "旧缓存" not in html
    assert db.scalar("SELECT readme_hash FROM project WHERE path=?", (proj,)) != old_hash


# --------------------------------- HTML 块内 Markdown / details（渲染口径 v4）
def test_readme_markdown_inside_html_block_is_parsed():
    """GitHub 行为对齐：HTML 块内的 **粗体** 要真的变粗，不是字面星号."""
    raw = ('<div align="center">\n\n**v105** · APP `0.45.0` · 上线\n\n</div>\n\n'
           '正文段落 **正常粗体**。\n')
    c, proj, _ = _setup({"README.md": raw})
    c.get("/project/demo")
    html = db.scalar("SELECT readme FROM project WHERE path=?", (proj,))
    assert "<strong>v105</strong>" in html, "div 内 markdown 没被解析"
    assert "<strong>正常粗体</strong>" in html
    assert "**v105**" not in html, "字面星号还留在输出里"
    assert 'markdown="1"' not in html, "注入的辅助属性不能残留到输出"


def test_readme_details_summary_kept():
    """折叠块是 README 常见用法，原生标签、无脚本，必须放行."""
    raw = ('<details>\n<summary>点开看清单</summary>\n\n- 甲\n- 乙\n\n</details>\n')
    c, proj, _ = _setup({"README.md": raw})
    c.get("/project/demo")
    html = db.scalar("SELECT readme FROM project WHERE path=?", (proj,))
    assert "<details>" in html
    assert "<summary>点开看清单</summary>" in html
    assert "<li>甲</li>" in html


def test_readme_md_attr_injection_no_dup_and_span_untouched():
    """已带 markdown 属性的不重复注入；span 这类内联标签不注入 markdown 属性."""
    from wb import security

    twice = security._inject_md_attr('<div markdown="1">**a**</div>')
    assert twice.count('markdown="1"') == 1
    injected = security._inject_md_attr('<p>**粗**</p>\n\n<span>**行内**</span>\n')
    assert '<p markdown="1">' in injected
    assert "<span markdown=" not in injected, "span 是行内标签，不该被注入"
    # span 不在白名单里会被剥掉，但里面的行内 markdown 照常解析（GitHub 同款）
    out = security.render_markdown('<p>**粗**</p>\n\n<span>**行内**</span>\n')
    assert "<strong>粗</strong>" in out
    assert "<strong>行内</strong>" in out
    assert "<span" not in out


# ------------------------------------------------------- 渲染缓存（hash）
def test_readme_hash_cache_three_steps():
    c, proj, _ = _setup({"README.md": "# 第一版\n\n内容 A\n"})
    c.get("/project/demo")
    h1 = db.scalar("SELECT readme_hash FROM project WHERE path=?", (proj,))
    html1 = db.scalar("SELECT readme FROM project WHERE path=?", (proj,))
    assert h1 and html1

    time.sleep(2)
    c.get("/project/demo")
    assert db.scalar("SELECT readme_hash FROM project WHERE path=?", (proj,)) == h1
    assert db.scalar("SELECT readme FROM project WHERE path=?", (proj,)) == html1

    with open(os.path.join(proj, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 第二版\n\n内容 B\n")
    body = _html(c.get("/project/demo"))
    h2 = db.scalar("SELECT readme_hash FROM project WHERE path=?", (proj,))
    assert h2 != h1, "改了 README 但 hash 没变"
    assert "第二版" in body, "页面还在显示过期缓存"


# ---------------------------------------------------------- 上传 / 删除
def test_upload_preview_saves_file_and_db():
    c, proj, _ = _setup({"README.md": "# 演示\n"})
    resp = c.post(
        "/api/project/preview",
        data={"rel": "demo", "data": "data:image/png;base64," + PNG_B64},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True, payload
    assert os.path.isfile(os.path.join(proj, "preview.png"))
    assert db.scalar("SELECT preview FROM project WHERE path=?", (proj,)) == "preview.png"


def test_upload_rejects_non_image():
    c, _, _ = _setup({"README.md": "# 演示\n"})
    resp = c.post("/api/project/preview", data={"rel": "demo", "data": "data:text/plain;base64,QUJD"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_upload_rejects_oversize():
    c, proj, _ = _setup({"README.md": "# 演示\n"})
    # base64 12MB → 解码后约 9MB，超过 8MiB 上限
    big = "data:image/png;base64," + "A" * 12_000_000
    resp = c.post("/api/project/preview", data={"rel": "demo", "data": big})
    assert resp.status_code == 400
    assert "8MB" in resp.get_json()["error"]
    assert not os.path.isfile(os.path.join(proj, "preview.png")), "超限不该落盘"


def test_delete_preview_returns_to_empty_state():
    c, proj, _ = _setup({"README.md": "# 演示\n"})
    c.post("/api/project/preview", data={"rel": "demo", "data": "data:image/png;base64," + PNG_B64})
    resp = c.post("/api/project/preview/delete", data={"rel": "demo"})
    assert resp.get_json()["ok"] is True
    assert not os.path.isfile(os.path.join(proj, "preview.png")), "文件没删掉"
    assert db.scalar("SELECT preview FROM project WHERE path=?", (proj,)) is None
    assert "还没有截图" in _html(c.get("/project/demo"))


# ------------------------------------------------------------ 路径安全
def test_path_traversal_rejected():
    c, _, root = _setup({"README.md": "# 演示\n"})
    outside = os.path.join(os.path.dirname(root), "outside.txt")
    with open(outside, "w", encoding="utf-8") as f:
        f.write("secret")
    resp = c.post("/api/project/preview", data={"rel": "../../outside.txt", "data": "data:image/png;base64," + PNG_B64})
    assert resp.status_code in (400, 404)
    assert resp.get_json()["ok"] is False


def test_unknown_project_404():
    c, _, _ = _setup({"README.md": "# 演示\n"})
    assert c.get("/project/not-exist").status_code == 404


def test_preview_without_preview_404():
    c, _, _ = _setup({"README.md": "# 演示\n"})
    assert c.get("/preview/demo").status_code == 404


# ------------------------------------------- README 内资源路由（本地相对图 / 链接）
def test_readme_asset_serves_local_image():
    """README 里的相对图片必须由应用自己服务，浏览器直接拼会 404."""
    from urllib.parse import quote

    c, proj, _ = _setup({"README.md": "# 演示\n"})
    os.makedirs(os.path.join(proj, "assets"), exist_ok=True)
    with open(os.path.join(proj, "assets", "logo.png"), "wb") as f:
        f.write(base64.b64decode(PNG_B64))
    resp = c.get("/readme-asset/demo?f=" + quote("assets/logo.png", safe="/"))
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_readme_asset_serves_svg_with_csp():
    """SVG 是本地自产物，放行；但直接打开时不能让它跑脚本."""
    c, proj, _ = _setup({"README.md": "# 演示\n"})
    with open(os.path.join(proj, "icon.svg"), "w", encoding="utf-8") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>')
    resp = c.get("/readme-asset/demo?f=icon.svg")
    assert resp.status_code == 200
    assert resp.mimetype == "image/svg+xml"
    assert "sandbox" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_readme_asset_serves_relative_doc_as_plain_text():
    """相对文档链接不能 404；本地没有 GitHub 文件页，如实给原始文本."""
    from urllib.parse import quote

    c, proj, _ = _setup({"README.md": "# 演示\n"})
    os.makedirs(os.path.join(proj, "docs"), exist_ok=True)
    with open(os.path.join(proj, "docs", "变更日志.md"), "w", encoding="utf-8") as f:
        f.write("# 变更日志\n\nv105 上线\n")
    resp = c.get("/readme-asset/demo?f=" + quote("docs/变更日志.md", safe="/"))
    assert resp.status_code == 200
    assert resp.mimetype.startswith("text/plain")
    assert "v105 上线" in resp.get_data(as_text=True)


def test_readme_asset_rejects_traversal_and_arbitrary_files():
    """这个路由不能变成任意文件读取口子."""
    c, proj, root = _setup({"README.md": "# 演示\n"})
    with open(os.path.join(os.path.dirname(root), "secret.txt"), "w", encoding="utf-8") as f:
        f.write("secret")
    with open(os.path.join(proj, "config.json"), "w", encoding="utf-8") as f:
        f.write('{"token":"should-not-leak"}')
    assert c.get("/readme-asset/demo?f=../../secret.txt").status_code == 404
    assert c.get("/readme-asset/demo?f=config.json").status_code == 404
    assert c.get("/readme-asset/demo?f=").status_code == 404
    assert c.get("/readme-asset/not-exist?f=icon.svg").status_code == 404


def test_readme_asset_rejects_symlink_escape():
    """项目内一个符号链接指向外部文件时，也必须挡住.

    abspath 只做字符串拼接，不解析链接；真实场景里 README 引用了
    一个指向仓库外的 logo.svg（或攻击者注入的链接）就会绕过白名单。
    Windows 上建 symlink 要管理员权限，失败时跳过并登记原因。
    """
    c, proj, root = _setup({"README.md": "# 演示\n"})
    outside = os.path.join(os.path.dirname(root), "outside.svg")
    with open(outside, "w", encoding="utf-8") as f:
        f.write("<svg>secret</svg>")
    link = os.path.join(proj, "leaked.svg")
    try:
        os.symlink(outside, link)
    except OSError:
        skip("Windows 上建符号链接需要管理员权限，无法构造符号链接逃逸用例")
    from urllib.parse import quote

    resp = c.get("/readme-asset/demo?f=" + quote("leaked.svg", safe="/"))
    assert resp.status_code == 404, "符号链接指向项目外必须被拒"
    assert "secret" not in resp.get_data(as_text=True), "不能读出链接目标的内容"


# ---------------------------------------------------------------- 413 与可视化（v8）
def test_shot_upload_accepts_image_over_500kb():
    """Werkzeug 3.1 的 MAX_FORM_MEMORY_SIZE 默认只有 500KB.

    截图走 base64（体积 ×1.33），一张 400KB 的图编码后 533KB 就撞线，
    表现是 413 + Werkzeug 原生 HTML 页 → 前端报「不是合法 JSON」。
    """
    import base64

    c, _proj, _root = _setup()
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * (400 * 1024)
    url = "data:image/png;base64," + base64.b64encode(blob).decode()
    resp = c.post("/api/project/preview", data={"rel": "demo", "data": url},
                  content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
    assert resp.get_json()["ok"] is True


def test_shot_upload_too_large_returns_json_not_html():
    """超 8MB 要回中文 JSON，不能是 Werkzeug 的 HTML 错误页."""
    import base64

    c, _proj, _root = _setup()
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * (9 * 1024 * 1024)
    url = "data:image/png;base64," + base64.b64encode(blob).decode()
    resp = c.post("/api/project/preview", data={"rel": "demo", "data": url},
                  content_type="multipart/form-data")
    assert resp.status_code in (400, 413), resp.status_code
    body = resp.get_json()
    assert body is not None, "接口必须回 JSON，不能回 HTML"
    assert body["ok"] is False
    assert "MB" in body["error"] or "上限" in body["error"], body["error"]


def test_detail_page_renders_health_and_rhythm():
    """详情页要有健康度和提交节奏两块 —— 不是一屏纯文字."""
    c, _proj, _root = _setup(files={"README.md": "# Demo\n\n一个测试项目。\n"})
    body = c.get("/project/demo").get_data(as_text=True)
    assert "wb-health" in body, "缺健康度区块"
    assert "wb-hitem" in body, "缺健康度维度条"
    assert "健康度" in body
    # 提交节奏：不是 git 仓库时不画假柱子，而是说明情况
    assert "提交节奏" in body


def test_detail_page_does_not_fake_commit_chart_for_non_git():
    c, _proj, _root = _setup(files={"README.md": "# Demo\n"})
    body = c.get("/project/demo").get_data(as_text=True)
    assert "wb-weeks" not in body, "非 git 项目不该画提交柱子（会全是空的，纯误导）"


def test_deploy_bar_exists_on_detail_page():
    """「部署到」栏被我重写模板时整块删掉过（CSS 在、HTML 没），这是回归，必须卡住."""
    c, _proj, _root = _setup(files={"README.md": "# Demo\n"})
    body = c.get("/project/demo").get_data(as_text=True)
    assert "wb-deploybar" in body, "部署栏不见了"
    assert "部署到" in body


def test_deploy_picker_uses_modal_and_is_visible_when_unlinked():
    """选服务器改成弹窗 + 未关联时整条高亮：小浮层那套不许再回来。"""
    c, _proj, _root = _setup(files={"README.md": "# Demo\n"})
    body = _html(c.get("/project/demo"))
    assert 'id="mbx-deploy"' in body, "部署弹窗不见了"
    assert 'data-mbx-open="mbx-deploy"' in body, "入口没接上弹窗"
    assert "wb-deploybar is-empty" in body, "未关联时整条该高亮成 CTA"
    assert "wb-deploymenu" not in body, "旧的下拉浮层又回来了"
    assert "data-deploy-toggle" not in body, "旧的下拉开合逻辑又回来了"


def test_deploy_modal_shows_real_host_port():
    """弹窗里要显示真实地址：后端只 select id,name 时会渲染成空的「:」。"""
    c, _proj, _root = _setup(files={"README.md": "# Demo\n"})
    with db.tx() as t:
        t.execute("INSERT INTO server(id,name,host,port,user,auth,secret,grp,note,"
                  "alert_mem,alert_disk,alert_load,enabled,created_at,updated_at) "
                  "VALUES(999,'测试机','1.2.3.4',22,'root','password','','','',85,90,4,1,?,?)",
                  (db.now(), db.now()))
    body = _html(c.get("/project/demo"))
    assert "1.2.3.4:22" in body, "服务器地址没渲染出来"
    assert 'data-link-srv="999"' in body and 'data-on="1"' in body, "弹窗里缺关联按钮"


def test_deploy_bar_lists_linked_servers_with_jump():
    c, _proj, _root = _setup(files={"README.md": "# Demo\n"})
    with db.tx() as t:
        t.execute("INSERT INTO server(id,name,host,port,user,auth,secret,grp,note,"
                  "alert_mem,alert_disk,alert_load,enabled,created_at,updated_at) "
                  "VALUES(999,'测试机','1.2.3.4',22,'root','password','','','',85,90,4,1,?,?)",
                  (db.now(), db.now()))
        t.execute("INSERT INTO project_server(project_path, server_id) VALUES(?,999)",
                  (_proj,))
    body = c.get("/project/demo").get_data(as_text=True)
    assert "data-go-server" in body or "chipgo" in body, "缺去服务器详情的入口"
    assert 'href="/server/999"' in body
