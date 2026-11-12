"""T15 引擎页验收：三档判定、口径可解释、手动收编/剔除/恢复、空库不炸.

口径（人拍板）：没有 README 的不算项目。
    auto       有 README        —— 自动收编，进项目列表
    candidate  有 .git / 清单    —— 是仓库但缺 README，等人定
    dropped    引擎减掉的        —— 不占列表，可恢复
"""

from __future__ import annotations

import os
import tempfile

from wb import create_app, db


def _setup(rows=None):
    root = tempfile.mkdtemp(prefix="wb-eng-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)

    for item in rows or []:
        with db.tx() as c:
            c.execute(
                """INSERT INTO project(path, dirname, name_auto, name_src, name_conf,
                                       stack, active, state, first_seen, last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["path"], item["dirname"], item.get("name", item["dirname"]),
                    item["src"], item.get("conf", 0.45), "Python", "活跃",
                    item.get("state", "auto"), db.now(), db.now(),
                ),
            )
    return app.test_client()


def _html(resp) -> str:
    return resp.get_data(as_text=True)


SAMPLE = [
    {"path": "D:/p/a", "dirname": "a", "src": "readme_h1", "conf": 0.9, "state": "auto"},
    {"path": "D:/p/b", "dirname": "b", "src": "readme_h1", "conf": 0.9, "state": "auto"},
    {"path": "D:/p/c", "dirname": "c", "src": "manifest", "conf": 0.72, "state": "candidate"},
    {"path": "D:/p/d", "dirname": "d", "src": "dirname", "conf": 0.45, "state": "dropped"},
]


def test_engine_page_renders_four_states():
    c = _setup(SAMPLE)
    resp = c.get("/engine")
    assert resp.status_code == 200
    body = _html(resp)
    for label in ("看过的目录", "自动收编", "待你定", "引擎剔除"):
        assert label in body, f"缺漏斗段：{label}"


def test_state_numbers_explainable():
    """4 条在册：2 收编 / 1 候选 / 1 剔除 —— 加起来必须等于总数."""
    c = _setup(SAMPLE)
    body = _html(c.get("/engine"))
    flat = body.replace("\n", "")
    assert ">4<" in flat, "总数不对"
    assert ">2<" in flat, "收编数不对"
    assert ">1<" in flat, "候选数不对"


def test_default_lands_on_candidates():
    """进引擎页先看「待你定」——那才是需要人动手的地方."""
    c = _setup(SAMPLE)
    body = _html(c.get("/engine"))
    assert "D:/p/c" in body
    assert "D:/p/a" not in body, "已收编的不该混在候选里"


def test_stage_filter_only_shows_that_stage():
    c = _setup(SAMPLE)
    cand = _html(c.get("/engine?stage=candidate"))
    assert "D:/p/c" in cand
    assert "D:/p/a" not in cand and "D:/p/d" not in cand

    dropped = _html(c.get("/engine?stage=dropped"))
    assert "D:/p/d" in dropped
    assert "D:/p/a" not in dropped and "D:/p/c" not in dropped


def test_rules_table_is_explainable():
    """页面上要能看到判定规则本身，不是黑盒."""
    c = _setup(SAMPLE)
    body = _html(c.get("/engine"))
    assert "README" in body
    assert "判断引擎" in body
    assert "按顺序匹配" in body


def test_adopt_candidate_moves_into_project_list():
    c = _setup(SAMPLE)
    resp = c.post("/api/engine/state", data={"path": "D:/p/c", "to": "auto"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert db.scalar("SELECT state FROM project WHERE path='D:/p/c'") == "auto"
    assert "D:/p/c" in _html(c.get("/projects")), "收编后要出现在项目列表"


def test_drop_removes_from_list_but_keeps_row():
    c = _setup(SAMPLE)
    resp = c.post("/api/engine/state", data={"path": "D:/p/a", "to": "dropped"})
    assert resp.json["changed"] == 1
    # 行还在（不删数据），只是 state 变了
    assert db.scalar("SELECT state FROM project WHERE path='D:/p/a'") == "dropped"
    assert "D:/p/a" not in _html(c.get("/projects")), "剔掉后不该在项目列表"


def test_restore_brings_it_back():
    c = _setup(SAMPLE)
    c.post("/api/engine/state", data={"path": "D:/p/d", "to": "auto"})
    assert "D:/p/d" in _html(c.get("/projects"))


def test_batch_accepts_multiple_paths():
    c = _setup(SAMPLE)
    resp = c.post("/api/engine/state", data={"paths": ["D:/p/c", "D:/p/d"], "to": "auto"})
    assert resp.json["changed"] == 2


def test_unknown_target_state_rejected():
    c = _setup(SAMPLE)
    resp = c.post("/api/engine/state", data={"path": "D:/p/c", "to": "whatever"})
    assert resp.status_code == 400
    assert resp.json["ok"] is False


def test_empty_target_rejected():
    c = _setup(SAMPLE)
    resp = c.post("/api/engine/state", data={"to": "auto"})
    assert resp.status_code == 400


def test_manual_named_item_stays_visible_even_if_dropped():
    """人改过名的不能被引擎藏起来 —— 人工认可优先于自动判定."""
    c = _setup(SAMPLE)
    db.execute("UPDATE project SET user_name='我改的名' WHERE path='D:/p/d'")
    assert "我改的名" in _html(c.get("/projects"))


def test_pager_hidden_when_few_rows():
    c = _setup(SAMPLE)
    assert "wb-pager" not in _html(c.get("/engine?stage=all"))


def test_empty_db_does_not_crash():
    c = _setup([])
    resp = c.get("/engine")
    assert resp.status_code == 200
    assert "没有待你定的候选" in _html(resp)
