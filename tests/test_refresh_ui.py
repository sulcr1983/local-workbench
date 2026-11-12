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


def test_servers_page_has_only_one_refresh_entry():
    """一个页面只留一个刷新入口（顶栏那颗），工具条里不能再重复一颗.

    0.2.2 之前两处都有：顶栏「刷新全部」扫项目、工具条「刷新全部」刷服务器，
    看起来像同一个东西，点下去干两件完全不同的事。
    """
    html = _html(_client().get("/servers"))
    assert 'data-act="servers"' in html, "顶栏那颗刷新入口没了"
    assert 'id="srv-refresh-all"' not in html, "工具条那颗重复的刷新按钮又回来了"
    assert 'id="srv-refresh-form"' not in html, "表单兜底也跟着回来了，刷新只该有一个入口"
    # 文案不能还指着已经删掉的按钮
    assert "刷新全部" not in html, "页面上还留着指向旧按钮的「刷新全部」文案"
    assert "<span>刷新服务器</span>" in html, "顶栏那颗按钮的文案不对"


def test_expiries_page_topbar_targets_expiries():
    """到期页的顶栏按钮要刷到期，不是扫项目."""
    html = _html(_client().get("/expiries"))
    assert 'data-act="expiries"' in html
    assert "<span>刷新到期</span>" in html


def test_expiries_page_has_only_one_refresh_entry():
    html = _html(_client().get("/expiries"))
    assert html.count("刷新全部") == 0, "到期页还留着重复的「刷新全部」按钮"


def test_projects_page_has_only_one_refresh_entry():
    """项目页工具条原来还有一颗「重新扫描」，和顶栏是同一个动作."""
    html = _html(_client().get("/projects"))
    assert 'data-act="projects"' in html
    assert "重新扫描" not in html, "工具条那颗重复的「重新扫描」又回来了"
