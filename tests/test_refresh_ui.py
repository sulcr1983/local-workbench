"""刷新按钮的契约：在服务器页刷服务器，在其它页扫项目，而且必须有反馈入口。

背景（2026-11-12 用户报的现象）：
- 服务器页顶栏那颗「刷新全部」调的是 /api/refresh-all —— 重扫项目目录，
  跟服务器一点关系没有（他说的「上面那个和服务器无关」）。
- 工具条那颗是同步表单提交，真连 4 台 SSH 约 7 秒，这期间页面毫无变化，
  用户以为按钮坏了（他说的「下面那个无效」）。

所以这里锁三件事：接口回 JSON、按钮按页面分流、表单有 JS 钩子。
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

from wb import create_app, db

STATS = {"total": 4, "online": 3, "written": 4, "skipped": 0}


def _client():
    root = tempfile.mkdtemp(prefix="wb-refresh-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)
    db.set_meta("onboarding_done", "1")
    return app.test_client()


def _html(resp) -> str:
    return resp.get_data(as_text=True)


def test_api_servers_refresh_returns_stats_as_json():
    """前端要先拿到结果才好提示「3/4 在线」，不能只给个 HTML 跳转."""
    with patch("wb.blueprints.servers.monitor.refresh", return_value=dict(STATS)):
        resp = _client().post("/api/servers/refresh")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["online"] == 3 and body["total"] == 4
    assert "written" in body


def test_servers_page_topbar_targets_servers():
    """服务器页的顶栏按钮必须是刷服务器，不能还是扫项目."""
    html = _html(_client().get("/servers"))
    assert 'data-act="servers"' in html
    assert "刷新服务器" in html


def test_other_pages_topbar_still_scans_projects():
    html = _html(_client().get("/projects"))
    assert 'data-act="projects"' in html
    assert "刷新服务器" not in html


def test_servers_page_refresh_form_is_hookable():
    """表单留着做无 JS 兜底，但得带 id，前端才能拦下来先给反馈."""
    html = _html(_client().get("/servers"))
    assert 'id="srv-refresh-form"' in html
    assert 'id="srv-refresh-all"' in html
