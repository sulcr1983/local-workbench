"""端到端路由覆盖：把审计脚本查出的 14 个缺口逐个补上.

原则（QA 视角）：
- 成功要验返回结构，不只验 200
- 失败要给可读中文，不能 500 或空体
- 破坏性操作要能拒绝非法输入
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

from wb import bootstrap, create_app, db


def _setup():
    root = tempfile.mkdtemp(prefix="wb-e2e-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)
    db.set_meta("onboarding_done", "1")
    return app.test_client(), root


def _html(resp) -> str:
    return resp.get_data(as_text=True)


def _seed_project(root: str, name: str = "demo") -> str:
    p = os.path.join(root, name)
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 演示项目\n\n这是说明。\n")
    return p


# ---------------------------------------------------------------- 审计页
def test_audit_page_renders():
    c, _ = _setup()
    resp = c.get("/audit")
    assert resp.status_code == 200
    body = _html(resp)
    assert "操作记录" in body


def test_audit_csv_export_has_bom_and_header():
    """导给 Excel 的 CSV 必须带 BOM，否则中文乱码."""
    c, _ = _setup()
    db.audit("测试动作", "随便一条")
    resp = c.get("/export/audit.csv")
    assert resp.status_code == 200
    raw = resp.data
    assert raw.startswith(b"\xef\xbb\xbf"), "缺 BOM，Excel 会乱码"
    text = raw.decode("utf-8-sig")
    assert "时间" in text and "操作" in text
    assert "测试动作" in text


# ---------------------------------------------------------------- 项目详情 / 截图
def test_project_detail_page_renders():
    c, root = _setup()
    path = _seed_project(root)
    with db.tx() as tx:
        tx.execute(
            "INSERT INTO project(path,dirname,name_auto,name_src,name_conf,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?,?)",
            (path, "demo", "演示项目", "readme_h1", 0.9, db.now(), db.now()),
        )
    with db.tx() as tx:
        tx.execute("INSERT INTO root(path, added_at) VALUES(?,?)", (root, db.now()))

    resp = c.get("/project/demo")
    assert resp.status_code == 200
    body = _html(resp)
    assert "演示项目" in body
    assert "README" in body


def test_preview_image_404_when_missing():
    c, root = _setup()
    _seed_project(root)
    with db.tx() as tx:
        tx.execute("INSERT INTO root(path, added_at) VALUES(?,?)", (root, db.now()))
    resp = c.get("/preview/demo")
    assert resp.status_code == 404, "没有截图该是 404，不是 500"


# ---------------------------------------------------------------- 项目扫描 / 根目录
def test_add_root_and_scan():
    c, root = _setup()
    target = _seed_project(root, "proj-a")
    assert not os.path.isfile(target + "/nope")

    resp = c.post("/projects/root", data={"path": root}, follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert db.scalar("SELECT COUNT(*) FROM root") == 1

    c.post("/projects/scan", follow_redirects=False)
    assert db.scalar("SELECT COUNT(*) FROM project") >= 1


def test_add_root_rejects_nonexistent_path():
    c, _ = _setup()
    c.post("/projects/root", data={"path": "Z:/definitely/not/here/"}, follow_redirects=False)
    assert db.scalar("SELECT COUNT(*) FROM root") == 0, "不存在的目录不该入库"


def test_refresh_all_redirects_and_writes_audit():
    c, _ = _setup()
    with patch("wb.monitor.refresh", lambda **k: {"total": 0, "online": 0, "written": 0, "skipped": 0}):
        resp = c.post("/servers/refresh", follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert db.scalar("SELECT COUNT(*) FROM audit WHERE action LIKE '%刷新%'") >= 1


# ---------------------------------------------------------------- 服务器单字段更新
def test_server_update_single_field():
    c, _ = _setup()
    with db.tx() as tx:
        tx.execute(
            "INSERT INTO server(name,host,port,user,auth,secret,created_at,updated_at) "
            "VALUES('s1','10.0.0.1',22,'root','password','pw',?,?)",
            (db.now(), db.now()),
        )
    resp = c.post("/api/server/update", data={"id": "1", "field": "note", "value": "改了备注"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert db.scalar("SELECT note FROM server WHERE id=1") == "改了备注"


def test_server_update_rejects_unknown_field():
    """字段白名单：前端传什么都不能让它改别的列."""
    c, _ = _setup()
    with db.tx() as tx:
        tx.execute(
            "INSERT INTO server(name,host,port,user,auth,secret,created_at,updated_at) "
            "VALUES('s1','10.0.0.1',22,'root','password','pw',?,?)",
            (db.now(), db.now()),
        )
    resp = c.post("/api/server/update", data={"id": "1", "field": "secret", "value": "hack"})
    assert resp.status_code == 400
    assert "不支持" in resp.get_json()["error"]


# ---------------------------------------------------------------- 通知
def test_notify_save_and_test_endpoint():
    c, _ = _setup()
    resp = c.post("/api/expiry/notify", data={
        "smtp_host": "", "smtp_port": "", "smtp_user": "", "smtp_pass": "",
        "mail_to": "", "webhook_type": "none", "webhook_url": "", "webhook_secret": "",
    })
    assert resp.status_code == 200 and resp.get_json()["ok"] is True

    resp2 = c.post("/api/expiry/notify/test")
    assert resp2.status_code == 200
    body = resp2.get_json()
    assert body["ok"] is False, "没配任何渠道时应当是「发不出去」，不能假装成功"
    assert body.get("detail") or body.get("results")


# ---------------------------------------------------------------- 云厂商测试解析
def test_provider_test_requires_config():
    c, _ = _setup()
    resp = c.post("/api/expiry/provider/test", data={"kind": "tencent", "cfg": "{}"})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is False
    assert "SecretId" in body["error"] or "密钥" in body["error"]


def test_provider_test_rejects_bad_json():
    c, _ = _setup()
    resp = c.post("/api/expiry/provider/test", data={"kind": "custom", "cfg": "not json"})
    assert resp.get_json()["ok"] is False


# ---------------------------------------------------------------- 向导
def test_welcome_discover_returns_shape():
    c, _ = _setup()
    with patch.object(bootstrap.discover, "ssh_candidates",
                      lambda: {"config": [], "keys": [], "known_hosts": [], "ssh_dir": ""}), \
         patch.object(bootstrap.discover, "project_hosts", lambda roots, scope="full": []):
        resp = c.post("/api/welcome/discover", data={"scope": "full"})
    body = resp.get_json()
    assert body["ok"] is True
    assert "ssh" in body and "project_hosts" in body


def test_welcome_done_marks_onboarding():
    c, _ = _setup()
    resp = c.post("/api/welcome/done")
    assert resp.status_code == 200
    assert db.get_meta("onboarding_done") == "1"


def test_welcome_skip_also_marks_done():
    """跳过是个跳转接口（回项目页），不是 JSON —— 但必须记住「跳过」这个决定."""
    c, _ = _setup()
    db.set_meta("onboarding_done", "")
    resp = c.post("/api/welcome/skip", follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert db.get_meta("onboarding_done") == "1", "跳过也要记住，不然每次进来都被拉去向导"


# ---------------------------------------------------------------- 产品视角：错误文案
def test_error_messages_are_human_readable():
    """所有失败返回都要是中文人话，不能是异常名或空串."""
    c, _ = _setup()
    cases = [
        ("/api/server/update", {"id": "1", "field": "secret", "value": "x"}),
        ("/api/server/reconnect", {"id": "999"}),
        ("/api/servers/import", {"items": "[]"}),
        ("/api/engine/state", {"path": "x", "to": "nope"}),
    ]
    for url, payload in cases:
        body = c.post(url, data=payload).get_json()
        assert body["ok"] is False
        err = body["error"]
        assert err and len(err) >= 4, f"{url} 错误信息太短：{err!r}"
        assert not err.startswith(("KeyError", "TypeError", "Traceback")), f"{url} 吐了异常名：{err}"
