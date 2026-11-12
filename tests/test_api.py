"""T7 写操作与批量验收：改名、置顶、批量归档、CSV 注入防护."""

from __future__ import annotations

import os
import tempfile

from wb import create_app, db


def _setup(n: int = 3):
    root = tempfile.mkdtemp(prefix="wb-api-")
    proj = os.path.join(root, "demo")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 演示项目\n")

    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)

    ts = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO root(path, added_at) VALUES(?,?)", (root, ts))
        for i in range(n):
            c.execute(
                """INSERT INTO project(path, dirname, name_auto, name_src, name_conf, stack,
                                       active, is_git, dirty, first_seen, last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (os.path.join(root, f"p{i}"), f"p{i}", f"项目 {i}", "dirname", 0.45,
                 "Python", "活跃", 0, 0, ts, ts),
            )
    return app.test_client(), root


def _html(resp) -> str:
    return resp.get_data(as_text=True)


def _make_project(root: str, name: str, readme: str = "# 标题\n") -> str:
    """在根目录下造一个真项目目录（重扫要读真实文件，不能只插库）."""
    proj = os.path.join(root, name)
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    return proj


# ---------------------------------------------------------------- 改名
def test_rename_overrides_and_badge_changes():
    c, _ = _setup()
    resp = c.post("/api/project/update", data={"rel": "p0", "field": "name", "value": "新名字"})
    assert resp.get_json()["ok"] is True

    body = _html(c.get("/projects"))
    assert "新名字" in body
    assert "手工命名" in body, "改名后来源应标成手工命名"


def test_rename_rejects_too_long():
    c, _ = _setup()
    resp = c.post("/api/project/update", data={"rel": "p0", "field": "name", "value": "x" * 200})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_rename_rejects_empty_name():
    """空名字不能"保存成功"：那会把 user_name 置 NULL、悄悄退回自动命名，
    前端却提示已保存 —— 用户以为改了，其实没改（E2E 实测发现）。"""
    c, _ = _setup()
    resp = c.post("/api/project/update", data={"rel": "p0", "field": "name", "value": "   "})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "不能为空" in body["error"]


def test_unknown_field_rejected():
    c, _ = _setup()
    resp = c.post("/api/project/update", data={"rel": "p0", "field": "readme", "value": "<script>"})
    assert resp.status_code == 400
    assert "不支持" in resp.get_json()["error"]


def test_invalid_status_rejected():
    c, _ = _setup()
    resp = c.post("/api/project/update", data={"rel": "p0", "field": "status", "value": "瞎写的状态"})
    assert resp.status_code == 400


# ---------------------------------------------------------------- 置顶
def test_toggle_pin():
    c, _ = _setup()
    assert c.post("/api/project/togglepin", data={"rel": "p0"}).get_json()["pinned"] is True
    assert c.post("/api/project/togglepin", data={"rel": "p0"}).get_json()["pinned"] is False


# ---------------------------------------------------------------- 批量
def test_bulk_archive_hides_from_active():
    c, root = _setup(3)
    paths = [os.path.join(root, f"p{i}") for i in range(3)]
    resp = c.post("/api/projects/bulk", data={"action": "archive", "paths": paths[:2]})
    assert resp.get_json()["count"] == 2

    active_body = _html(c.get("/projects?flt=active"))
    assert "项目 0" not in active_body
    assert "项目 2" in active_body

    archived = _html(c.get("/projects?flt=archived"))
    assert "项目 0" in archived and "项目 2" not in archived


def test_bulk_requires_selection():
    c, _ = _setup()
    resp = c.post("/api/projects/bulk", data={"action": "pin"})
    assert resp.status_code == 400
    assert "没有选中" in resp.get_json()["error"]


def test_bulk_rejects_unknown_action():
    c, root = _setup()
    resp = c.post("/api/projects/bulk", data={"action": "drop-table", "paths": [os.path.join(root, "p0")]})
    assert resp.status_code == 400


def test_bulk_pin():
    c, root = _setup(2)
    c.post("/api/projects/bulk", data={"action": "pin", "paths": [os.path.join(root, "p0")]})
    assert db.scalar("SELECT user_pinned FROM project WHERE dirname='p0'") == 1


# ---------------------------------------------------------------- CSV
def test_csv_escapes_formula_injection():
    c, root = _setup(1)
    db.execute(
        "UPDATE project SET user_name=?, user_note=? WHERE path=?",
        ("=1+1", "@SUM(A1)", os.path.join(root, "p0")),
    )
    resp = c.get("/export/projects.csv")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "'=1+1" in text, "= 开头的字段必须前置单引号"
    assert "'@SUM(A1)" in text


def test_csv_keeps_legit_negative_numbers():
    """-2、-v2 是合法值，不该被转义成 '-2."""
    c, root = _setup(1)
    db.execute("UPDATE project SET user_note=? WHERE path=?", ("-2 天", os.path.join(root, "p0")))
    text = c.get("/export/projects.csv").get_data(as_text=True)
    assert "-2 天" in text
    assert "'-2 天" not in text


def test_csv_has_bom_for_excel():
    c, _ = _setup(1)
    raw = c.get("/export/projects.csv").get_data()
    assert raw.startswith(b"\xef\xbb\xbf"), "Excel 需要 BOM 才不乱码"

def test_bulk_ignore_hides_from_default_list():
    """「从清单移除」= 标记忽略，不删数据；默认列表看不到，已忽略筛选里能捞回来."""
    c, root = _setup(2)
    path = os.path.join(root, "p0")
    c.post("/api/projects/bulk", data={"action": "ignore", "paths": [path]})

    default_body = _html(c.get("/projects"))
    assert "项目 0" not in default_body
    assert "项目 1" in default_body

    ignored = _html(c.get("/projects?flt=ignored"))
    assert "项目 0" in ignored
    assert db.scalar("SELECT user_status FROM project WHERE path=?", (path,)) == "忽略"


def test_bulk_unarchive_restores_ignored():
    c, root = _setup(1)
    path = os.path.join(root, "p0")
    c.post("/api/projects/bulk", data={"action": "ignore", "paths": [path]})
    c.post("/api/projects/bulk", data={"action": "unarchive", "paths": [path]})
    assert "项目 0" in _html(c.get("/projects"))


def test_open_rejects_unknown_project():
    c, _ = _setup(1)
    resp = c.post("/api/open", data={"rel": "definitely-not-here"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ---------------------------------------------------------------- 刷新（v9）
def test_topbar_refresh_button_is_not_disabled():
    """「刷新全部」之前是 disabled 的摆设 —— 这个回归必须卡住."""
    c, _ = _setup()
    body = c.get("/projects").get_data(as_text=True)
    assert 'id="wb-refresh"' in body
    # 按钮标签里不能出现 disabled
    import re

    m = re.search(r'<button[^>]*id="wb-refresh"[^>]*>', body)
    assert m, "找不到刷新按钮"
    assert "disabled" not in m.group(0), m.group(0)


def test_refresh_all_endpoint_scans_roots():
    c, root = _setup()
    resp = c.post("/api/refresh-all")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j["ok"] is True
    assert "scanned" in j


def test_refresh_all_without_roots_is_readable():
    c, _ = _setup()
    with db.tx() as t:
        t.execute("DELETE FROM root")
    resp = c.post("/api/refresh-all")
    assert resp.status_code == 400
    assert "项目文件夹" in resp.get_json()["error"]


def test_project_rescan_updates_that_one_project():
    """单个项目重扫要真把改动读进来 —— 这是用户报「改了也没变动」的核心诉求."""
    c, root = _setup()
    proj = _make_project(root, "p1", readme="# 旧标题\n")
    c.post("/api/refresh-all")

    # 改 README 后单独重扫
    with open(os.path.join(proj, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 新标题\n\n换了一段描述。\n")

    resp = c.post("/api/project/rescan", data={"rel": "p1"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["ok"] is True

    row = db.query_one("SELECT * FROM project WHERE dirname='p1'")
    assert row is not None
    assert "新标题" in (row["name_auto"] or "") or "新标题" in (row["summary"] or ""), dict(row)


def test_project_rescan_unknown_rel_is_404():
    c, _ = _setup()
    resp = c.post("/api/project/rescan", data={"rel": "nope"})
    assert resp.status_code in (400, 404)
    assert resp.get_json()["ok"] is False


def test_project_rescan_keeps_locked_state():
    """人拍过板的判定，单项目重扫也不许覆盖."""
    c, root = _setup()
    _make_project(root, "p2", readme="# x\n")
    c.post("/api/refresh-all")
    with db.tx() as t:
        t.execute("UPDATE project SET state='dropped', state_locked=1 WHERE dirname='p2'")
    c.post("/api/project/rescan", data={"rel": "p2"})
    row = db.query_one("SELECT state FROM project WHERE dirname='p2'")
    assert row["state"] == "dropped", "锁定状态被覆盖了"
