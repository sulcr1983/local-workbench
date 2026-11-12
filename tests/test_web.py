"""T5 项目列表页路由验收（Flask test client，不启服务）."""

from __future__ import annotations

import os
import tempfile

from wb import create_app, db


def _client(projects: int = 0) -> object:
    d = tempfile.mkdtemp(prefix="wb-web-")
    db.close_thread_conn()
    db.configure(os.path.join(d, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)

    if projects:
        ts = db.now()
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO root(path, added_at) VALUES(?,?)", ("D:/proj", ts))
            for i in range(1, projects + 1):
                c.execute(
                    """INSERT INTO project(path, dirname, name_auto, name_src, name_conf,
                                           stack, active, is_git, dirty, first_seen, last_seen)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"D:/proj/proj-{i:02d}", f"proj-{i:02d}", f"项目 {i:02d}",
                        "dirname", 0.45, "Python", "活跃", 0, 0, ts, ts,
                    ),
                )
    return app.test_client()


def _html(resp) -> str:
    return resp.get_data(as_text=True)


def test_projects_page_ok():
    c = _client(30)
    resp = c.get("/projects")
    assert resp.status_code == 200
    body = _html(resp)
    assert body.count('class="wb-pnm"') == 12, "首页应是 12 条"
    assert "项目 01" in body


def test_page_2_shows_next_batch():
    c = _client(30)
    body = _html(c.get("/projects?page=2"))
    assert body.count('class="wb-pnm"') == 12
    assert "项目 13" in body and "项目 01<" not in body


def test_page_99_lands_on_last():
    c = _client(30)
    body = _html(c.get("/projects?page=99"))
    assert body.count('class="wb-pnm"') == 6, "末页应是 6 条，不能是空表"
    assert "项目 30" in body


def test_pager_hidden_when_single_page():
    c = _client(5)
    body = _html(c.get("/projects"))
    assert "wb-pager" not in body, "只有一页时不该显示分页条"


def test_pager_prev_disabled_on_first_page():
    c = _client(30)
    body = _html(c.get("/projects"))
    assert "wb-pg is-off" in body and "上一页" in body


def test_pager_next_disabled_on_last_page():
    c = _client(30)
    body = _html(c.get("/projects?page=3"))
    assert "wb-pg is-off" in body and "下一页" in body


def test_query_params_survive_paging():
    c = _client(30)
    body = _html(c.get("/projects?q=项目&flt=active&sort=name&page=2"))
    assert "flt=active" in body and "sort=name" in body and "q=%E9%A1%B9%E7%9B%AE" in body


def test_filter_narrows_result():
    c = _client(30)
    # LIKE '%proj-0%' 命中 proj-01..proj-09
    db.execute("UPDATE project SET active='停滞' WHERE path LIKE '%proj-0%'")
    body = _html(c.get("/projects?flt=stale"))
    assert "项目 09" in body
    assert "项目 10" not in body, "不在筛选条件里的项目不该出现"
    assert "项目 01" in body


def test_search_filters_by_name():
    c = _client(30)
    body = _html(c.get("/projects?q=项目 07"))
    assert "项目 07" in body
    assert "项目 08" not in body


def test_empty_state_when_no_roots():
    c = _client(0)
    body = _html(c.get("/projects"))
    assert "先把项目文件夹加进来" in body


def test_nav_link_points_to_projects_blueprint():
    """侧栏不能指着已被移除的 pages.projects，否则整页 500."""
    c = _client(0)
    body = _html(c.get("/settings"))
    assert 'href="/projects"' in body


def test_404_page_is_rendered_not_blank():
    c = _client(0)
    resp = c.get("/definitely-not-a-page")
    assert resp.status_code == 404
    body = _html(resp)
    assert "标签" not in body
    assert "页面不存在" in body or "找不到页面" in body
