"""配置导入的入参契约.

前端用 FormData 的 payload 字段提交；脚本/curl 直接发 JSON body 也应当能用。
最关键的是第三种情况：什么都没发的时候必须明确报错 —— 以前会静默返回
ok:true + 0/0，界面上写着「导入完成：服务器 0 台 · 项目 0 个」，
用户以为成功了，其实一个字段都没进来（静默失效）。
"""

from __future__ import annotations

import json
import os
import tempfile

from wb import create_app, db

PAYLOAD = {
    "version": 1,
    "roots": [],
    "servers": [
        {"name": "demo-1", "host": "10.0.0.1", "port": 22, "user": "root",
         "auth": "key_path", "note": "测试机"},
    ],
    "projects": {},
    "notify": {},
}


def _client():
    root = tempfile.mkdtemp(prefix="wb-import-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)
    db.set_meta("onboarding_done", "1")
    return app.test_client()


def test_import_rejects_empty_payload_loudly():
    c = _client()
    resp = c.post("/api/config/import")
    assert resp.status_code == 400, "空提交必须报错，不能假装成功"
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"], "要给出人能看懂的原因"


def test_import_accepts_json_body():
    c = _client()
    resp = c.post("/api/config/import", json=PAYLOAD)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["servers"] == 1, body
    row = db.query_one("SELECT name, host FROM server WHERE name=?", ("demo-1",))
    assert row is not None and row["host"] == "10.0.0.1"


def test_import_accepts_form_payload():
    """前端实际走这条：FormData.append('payload', 文件文本)."""
    c = _client()
    resp = c.post("/api/config/import",
                  data={"payload": json.dumps(PAYLOAD)}, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["servers"] == 1


def test_import_rejects_broken_json_in_form():
    c = _client()
    resp = c.post("/api/config/import", data={"payload": "{不是 json"},
                  content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
