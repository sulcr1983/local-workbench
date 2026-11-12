"""错误处理的契约测试：接口一律 JSON，页面一律中文 HTML。

口径来自项目自己的注释（413 那段）：前端有十几个裸 `r.json()`，
后端一旦吐 Werkzeug 的英文 HTML 错误页，UI 会静默失败 ——
用户只看到「服务返回的不是合法 JSON」。这条规则以前只落在 413 上。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from wb import create_app, db

BAD_MARKERS = ("Method Not Allowed", "The method is not allowed",
               "The requested URL was not found")


def _client(testing: bool = False):
    root = tempfile.mkdtemp(prefix="wb-err-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=testing)
    db.set_meta("onboarding_done", "1")

    # 故意抛异常的两个探针：一个接口路径、一个页面路径
    @app.get("/api/_probe_boom")
    def _api_boom():
        raise RuntimeError("probe")

    @app.get("/_probe_boom")
    def _page_boom():
        raise RuntimeError("probe")

    return app.test_client()


def _json(resp) -> dict:
    assert resp.mimetype == "application/json", resp.headers.get("Content-Type")
    return json.loads(resp.get_data(as_text=True))


# ---------------------------------------------------------------- 接口侧


def test_api_404_is_json():
    body = _json(_client().get("/api/definitely-not-here"))
    assert body["ok"] is False
    assert body["error"]


def test_api_405_is_json():
    """浏览器直接打开只收 POST 的接口会走这里."""
    body = _json(_client().post("/api/fs/drives"))
    assert body["ok"] is False
    assert body["error"]


def test_api_500_is_json_with_error_id():
    """接口炸了也要回 JSON，且带编号方便对日志."""
    logging.getLogger("wb").setLevel(logging.CRITICAL)  # 别把栈打到测试输出
    try:
        resp = _client().get("/api/_probe_boom")
    finally:
        logging.getLogger("wb").setLevel(logging.NOTSET)
    body = _json(resp)
    assert body["ok"] is False
    assert "编号" in body["error"], body


# ---------------------------------------------------------------- 页面侧


def test_page_404_is_html_and_chinese():
    resp = _client().get("/definitely-not-here")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["Content-Type"]
    assert "找不到页面" in resp.get_data(as_text=True)


def test_page_405_is_html_and_chinese():
    """不能落到 Werkzeug 的英文默认页上."""
    resp = _client().get("/projects/scan")
    assert resp.status_code == 405
    text = resp.get_data(as_text=True)
    assert "text/html" in resp.headers["Content-Type"]
    assert "打开方式不对" in text
    for bad in BAD_MARKERS:
        assert bad not in text, f"泄漏了 Werkzeug 默认页文案：{bad}"


def test_page_500_is_html_with_error_id():
    logging.getLogger("wb").setLevel(logging.CRITICAL)
    try:
        resp = _client().get("/_probe_boom")
    finally:
        logging.getLogger("wb").setLevel(logging.NOTSET)
    assert resp.status_code == 500
    text = resp.get_data(as_text=True)
    assert "text/html" in resp.headers["Content-Type"]
    assert "Traceback" not in text, "页面上不许出现堆栈"
    assert "出错了" in text


# ---------------------------------------------------------------- 导出头


def test_csv_exports_have_single_charset():
    """`mimetype="text/csv; charset=utf-8"` 会让 charset 出现两次，必须用 content_type."""
    c = _client()
    for url in ("/export/projects.csv", "/export/audit.csv"):
        ct = c.get(url).headers["Content-Type"]
        assert ct.lower().count("charset") == 1, f"{url} 的 Content-Type 重复：{ct}"
        assert "text/csv" in ct
