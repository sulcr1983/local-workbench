"""新手向导与本机发现验收."""

from __future__ import annotations

import os
import tempfile

from wb import create_app, db, discover


def _client(setup_done: bool = False):
    root = tempfile.mkdtemp(prefix="wb-wz-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    if setup_done:
        db.set_meta("onboarding_done", "1")
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client(), root


def _html(resp) -> str:
    return resp.get_data(as_text=True)


# ---------------------------------------------------------------- 入口
def test_first_visit_goes_to_welcome():
    c, _ = _client()
    resp = c.get("/")
    assert resp.status_code == 302
    assert "/welcome" in resp.headers["Location"]


def test_welcome_page_renders():
    c, _ = _client()
    body = _html(c.get("/welcome"))
    assert "选项目文件夹" in body
    assert "不设边界" in body, "必须把扫描范围写明"


def test_after_done_root_goes_to_projects():
    c, _ = _client(setup_done=True)
    resp = c.get("/")
    assert resp.status_code == 302
    assert "/projects" in resp.headers["Location"]


# ---------------------------------------------------------------- 目录浏览
def test_drives_and_list():
    c, root = _client()
    drives = c.get("/api/fs/drives").get_json()
    assert drives["ok"] and drives["drives"]

    os.makedirs(os.path.join(root, "proj-a"), exist_ok=True)
    os.makedirs(os.path.join(root, "proj-b"), exist_ok=True)
    resp = c.get(f"/api/fs/list?path={root}")
    payload = resp.get_json()
    assert payload["ok"]
    names = [d["name"] for d in payload["dirs"]]
    assert "proj-a" in names and "proj-b" in names


def test_list_rejects_missing_dir():
    c, _ = _client()
    resp = c.get("/api/fs/list?path=Z:/definitely-not-here")
    assert resp.status_code == 400


# ---------------------------------------------------------------- 多选根目录
def test_add_multiple_roots_at_once():
    c, root = _client()
    a = os.path.join(root, "a")
    b = os.path.join(root, "b")
    for d in (a, b):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("flask\n")

    resp = c.post("/api/welcome/roots", data={"paths": [a, b]})
    payload = resp.get_json()
    assert payload["ok"] is True
    assert len(payload["added"]) == 2
    assert db.scalar("SELECT COUNT(*) FROM root") == 2
    assert payload["scanned"] == 2


def test_add_roots_reports_bad_path():
    c, root = _client()
    good = os.path.join(root, "ok")
    os.makedirs(good, exist_ok=True)
    payload = c.post("/api/welcome/roots", data={"paths": [good, "Z:/nope"]}).get_json()
    assert payload["ok"] is True
    assert payload["skipped"] == ["Z:/nope"]


# ---------------------------------------------------------------- 扫描范围
def test_scope_saved_and_used():
    c, _ = _client()
    assert c.post("/api/welcome/scope", data={"scope": "safe"}).get_json()["ok"] is True
    from wb.blueprints.settings import load_ui

    assert load_ui()["scan_scope"] == "safe"

    bad = c.post("/api/welcome/scope", data={"scope": "whatever"})
    assert bad.status_code == 400


def test_project_hosts_scope_filters_files():
    root = tempfile.mkdtemp(prefix="wb-scan-")
    proj = os.path.join(root, "p1")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "README.md"), "w", encoding="utf-8") as f:
        f.write("部署到 10.1.2.3 这台机器\n")
    with open(os.path.join(proj, ".env"), "w", encoding="utf-8") as f:
        f.write("DB_HOST=10.9.9.9\n")

    full = {h["host"] for h in discover.project_hosts([root], scope="full")}
    safe = {h["host"] for h in discover.project_hosts([root], scope="safe")}
    assert "10.1.2.3" in full and "10.1.2.3" in safe
    assert "10.9.9.9" in full, "无限制模式应该读到 .env"
    assert "10.9.9.9" not in safe, "保守模式不该碰 .env"


# ---------------------------------------------------------------- 导入服务器
def test_import_servers_from_candidates():
    c, _ = _client()
    items = [
        {"name": "host-a", "host": "10.0.0.1", "port": 22, "user": "root", "key": "C:/k/id_rsa"},
        {"name": "host-b", "host": "10.0.0.2", "port": 2222, "user": "ubuntu", "key": ""},
    ]
    import json as _json

    payload = c.post("/api/welcome/servers", data={"items": _json.dumps(items)}).get_json()
    assert payload["created"] == 2
    row = db.query_one("SELECT * FROM server WHERE name='host-a'")
    assert row["auth"] == "key_path" and row["secret"] == "C:/k/id_rsa"
    row2 = db.query_one("SELECT * FROM server WHERE name='host-b'")
    assert row2["auth"] == "agent"


def test_import_servers_rejects_empty():
    c, _ = _client()
    assert c.post("/api/welcome/servers", data={"items": "[]"}).status_code == 400


# ---------------------------------------------------------------- 部署关联
def test_project_server_link_toggle():
    c, root = _client(setup_done=True)
    proj = os.path.join(root, "demo")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 演示\n")

    ts = db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO root(path, added_at) VALUES(?,?)", (root, ts))
        conn.execute(
            """INSERT INTO project(path, dirname, name_auto, first_seen, last_seen)
               VALUES(?,?,?,?,?)""",
            (proj, "demo", "演示", ts, ts),
        )
        conn.execute(
            "INSERT INTO server(name, host, created_at, updated_at) VALUES('s1','1.1.1.1',?,?)",
            (ts, ts),
        )

    assert c.post("/api/project/server", data={"rel": "demo", "sid": "1", "on": "1"}).get_json()["ok"]
    assert db.scalar("SELECT COUNT(*) FROM project_server") == 1

    assert c.post("/api/project/server", data={"rel": "demo", "sid": "1", "on": "0"}).get_json()["ok"]
    assert db.scalar("SELECT COUNT(*) FROM project_server") == 0


# ------------------------------------------------ 公共站点不该当服务器
def test_code_hosts_filtered_from_known_hosts():
    """known_hosts 里的 github.com 不是要巡检的服务器 —— 之前漏在这没过滤."""
    assert discover.is_not_a_server("github.com")
    assert discover.is_not_a_server("ssh.github.com"), "子域也算公共站点"
    assert discover.is_not_a_server("mail.qq.com")
    assert not discover.is_not_a_server("203.0.113.10")
    assert not discover.is_not_a_server("node-a.test")


def test_project_hosts_skips_public_sites():
    root = tempfile.mkdtemp(prefix="wb-host-")
    proj = os.path.join(root, "demo")
    os.makedirs(proj)
    with open(os.path.join(proj, "README.md"), "w", encoding="utf-8") as f:
        f.write(
            "部署到 root@203.0.113.10\n"
            "代码在 https://github.com/foo/bar\n"
            "文档 mail.qq.com\n"
            "备用 192.168.1.20\n"
        )
    hosts = {h["host"] for h in discover.project_hosts([root], scope="full")}
    assert "203.0.113.10" in hosts
    assert "192.168.1.20" in hosts
    assert "github.com" not in hosts
    assert "qq.com" not in hosts, "公共站点不该出现在候选里"
