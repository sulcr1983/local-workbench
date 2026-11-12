"""T16 设置页验收：开关、根目录、活跃度口径、导出脱敏、导入合并."""

from __future__ import annotations

import json
import os
import tempfile

from wb import create_app, db


def _setup():
    root = tempfile.mkdtemp(prefix="wb-set-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client(), root


def _html(resp) -> str:
    return resp.get_data(as_text=True)


def test_settings_page_renders():
    c, _ = _setup()
    body = _html(c.get("/settings"))
    assert "设置" in body
    assert "扫描根目录" in body
    assert "配置搬家" in body


def test_toggle_show_overview():
    c, _ = _setup()
    from wb.blueprints.settings import load_ui

    assert load_ui()["show_overview"] is False
    resp = c.post("/api/ui/toggle", data={"key": "show_overview", "value": "1"})
    assert resp.get_json()["ok"] is True
    assert load_ui()["show_overview"] is True

    c.post("/api/ui/toggle", data={"key": "show_overview", "value": "0"})
    assert load_ui()["show_overview"] is False


def test_toggle_rejects_unknown_key():
    c, _ = _setup()
    assert c.post("/api/ui/toggle", data={"key": "drop_all", "value": "1"}).status_code == 400


def test_overview_redirects_when_disabled():
    c, _ = _setup()
    db.set_meta("onboarding_done", "1")   # 跳过新手向导，直接看首页行为
    resp = c.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/projects")


def test_rules_validation():
    c, _ = _setup()
    bad = c.post("/api/settings/rules", data={"active": 30, "slow": 7, "stale": 90})
    assert bad.status_code == 400
    assert "从小到大" in bad.get_json()["error"]

    ok = c.post("/api/settings/rules", data={"active": 3, "slow": 20, "stale": 60})
    assert ok.get_json()["ok"] is True


def test_root_add_and_delete_keeps_disk():
    c, root = _setup()
    target = os.path.join(root, "proj-root")
    os.makedirs(target, exist_ok=True)

    assert c.post("/api/root/add", data={"path": target}).get_json()["ok"] is True
    assert db.scalar("SELECT COUNT(*) FROM root") == 1

    assert c.post("/api/root/del", data={"path": target}).get_json()["ok"] is True
    assert db.scalar("SELECT COUNT(*) FROM root") == 0
    assert os.path.isdir(target), "删配置不该动磁盘目录"


def test_root_add_rejects_missing_dir():
    c, _ = _setup()
    resp = c.post("/api/root/add", data={"path": r"Z:\definitely-not-here"})
    assert resp.status_code == 400
    assert "不存在" in resp.get_json()["error"]


# ---------------------------------------------------------------- 导出
def test_export_masks_credentials():
    c, _ = _setup()
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO server(name, host, user, auth, secret, passphrase, created_at, updated_at)
               VALUES('机器1','1.2.3.4','root','password','MYSECRET','PASS123',?,?)""",
            (db.now(), db.now()),
        )
    data = json.loads(_html(c.get("/api/config/export")))
    # 白名单：凭据列根本不出现，而不是「出现了但空串」——
    # 后者意味着以后哪天代码改回去，导出立刻带出明文。
    assert "secret" not in data["servers"][0], "导出里不该出现 secret 字段"
    assert "passphrase" not in data["servers"][0], "导出里不该出现 passphrase 字段"
    assert data["notify"]["smtp_pass"] == ""
    assert "MYSECRET" not in json.dumps(data, ensure_ascii=False)


def test_export_masks_url_tokens():
    c, _ = _setup()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO notify_cfg(id, cfg) VALUES(1, ?)",
            (json.dumps({
                "webhook_type": "dingtalk",
                "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=SEALPHONE123ABC",
            }, ensure_ascii=False),),
        )
    data = json.loads(_html(c.get("/api/config/export")))
    url = data["notify"]["webhook_url"]
    assert "SEALPHONE123ABC" not in url, "导出的 webhook_url 必须掩码 access_token"
    assert "access_token" in url, "保留参数名，方便用户知道这里原来填过东西"


def test_export_has_structure():
    c, _ = _setup()
    data = json.loads(_html(c.get("/api/config/export")))
    for key in ("version", "roots", "ui", "servers", "projects"):
        assert key in data


# ---------------------------------------------------------------- 导入
def test_import_updates_but_keeps_secret():
    c, _ = _setup()
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO server(name, host, user, auth, secret, created_at, updated_at)
               VALUES('机器1','1.1.1.1','root','password','OLD_PW',?,?)""",
            (db.now(), db.now()),
        )

    payload = {
        "servers": [
            {"name": "机器1", "host": "9.9.9.9", "user": "root", "auth": "password", "secret": ""}
        ]
    }
    resp = c.post("/api/config/import", data={"payload": json.dumps(payload)})
    assert resp.get_json()["ok"] is True
    assert db.scalar("SELECT host FROM server WHERE name='机器1'") == "9.9.9.9"
    assert db.scalar("SELECT secret FROM server WHERE name='机器1'") == "OLD_PW", "空密码不能覆盖旧密码"


def test_import_adds_new_server():
    c, _ = _setup()
    payload = {"servers": [{"name": "新机器", "host": "2.2.2.2", "user": "root",
                            "auth": "password", "secret": "pw"}]}
    resp = c.post("/api/config/import", data={"payload": json.dumps(payload)})
    assert resp.get_json()["servers"] == 1
    assert db.scalar("SELECT COUNT(*) FROM server") == 1


def test_import_projects_merge_manual_fields():
    c, _ = _setup()
    path = r"D:\test\demo"
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO project(path, dirname, first_seen, last_seen)
               VALUES(?,?,?,?)""",
            (path, "demo", db.now(), db.now()),
        )
    payload = {"projects": {path: {"name": "手工名", "status": "进行中", "note": "等接口", "pinned": True}}}
    resp = c.post("/api/config/import", data={"payload": json.dumps(payload)})
    assert resp.get_json()["projects"] == 1
    row = db.query_one("SELECT user_name, user_status, user_note, user_pinned FROM project WHERE path=?", (path,))
    assert row["user_name"] == "手工名"
    assert row["user_status"] == "进行中"
    assert row["user_pinned"] == 1


def test_import_rejects_bad_json():
    c, _ = _setup()
    resp = c.post("/api/config/import", data={"payload": "{not json"})
    assert resp.status_code == 400
    assert "JSON" in resp.get_json()["error"]
