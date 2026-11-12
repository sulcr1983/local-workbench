"""T13 到期页验收：5 档分级、分页、手动项、刷新链路."""

from __future__ import annotations

import os
import tempfile

from wb import create_app, db, expiry


def _setup(rows: int = 0):
    root = tempfile.mkdtemp(prefix="wb-exp-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)

    with db.tx() as c:
        for i in range(rows):
            c.execute(
                """INSERT INTO expiry(kind, name, expire, renew, note, provider, last_err, updated_at)
                   VALUES('manual', ?, date('now','localtime',?), 0, '', '', '', ?)""",
                (f"条目 {i:02d}", f"+{i} day", db.now()),
            )
    return app.test_client()


def _html(resp) -> str:
    return resp.get_data(as_text=True)


# ---------------------------------------------------------------- 5 档
def test_level_five_bands():
    assert expiry.level(-3)[1] == "err"
    assert "已过期" in expiry.level(-3)[0]
    assert expiry.level(0)[1] == "err"
    assert expiry.level(7)[0] == "7 天内"
    assert expiry.level(8)[1] == "warn"
    assert expiry.level(15)[0] == "15 天内"
    assert expiry.level(16)[1] == "yellow"
    assert expiry.level(30)[0] == "30 天内"
    assert expiry.level(31)[1] == "ok"
    assert expiry.level(None) == ("未采集", "mut")


def test_days_left_parsing():
    assert expiry.days_left("") is None
    assert expiry.days_left("不是日期") is None
    assert isinstance(expiry.days_left("2099-01-01"), int)


# ---------------------------------------------------------------- 页面
def test_page_renders_rows():
    c = _setup(3)
    body = _html(c.get("/expiries"))
    assert "条目 00" in body
    assert "到期提醒" in body


def test_single_page_hides_pager():
    c = _setup(5)
    assert "wb-pager" not in _html(c.get("/expiries"))


def test_pagination_two_pages():
    c = _setup(12)
    body = _html(c.get("/expiries"))
    assert "wb-pager" in body
    assert body.count("条目 ") == 10, "每页应是 10 条"


def test_filter_urgent_only_30_days():
    c = _setup(40)  # 0..39 天
    body = _html(c.get("/expiries?flt=urgent"))
    assert "条目 00" in body
    assert "条目 39" not in body


# ---------------------------------------------------------------- 手动项
def test_add_manual_item():
    c = _setup(0)
    resp = c.post("/api/expiry/add", data={"kind": "manual", "name": "云服务器续费", "expire": "2099-01-01"})
    assert resp.get_json()["ok"] is True
    assert db.scalar("SELECT COUNT(*) FROM expiry") == 1


def test_add_rejects_bad_date():
    c = _setup(0)
    resp = c.post("/api/expiry/add", data={"kind": "manual", "name": "x", "expire": "下个月"})
    assert resp.status_code == 400
    assert "格式" in resp.get_json()["error"]


def test_add_rejects_overlong_name():
    """名称直接进列表首列且无截断，不清长度会把表格撑破（E2E 实测 300 字撑变形）."""
    c = _setup(0)
    resp = c.post("/api/expiry/add", data={"kind": "manual", "name": "X" * 300, "expire": "2099-01-01"})
    assert resp.status_code == 400
    assert "120" in resp.get_json()["error"]
    assert db.scalar("SELECT COUNT(*) FROM expiry") == 0, "被拒绝时不能落库"


def test_delete_manual_item():
    c = _setup(1)
    eid = db.scalar("SELECT id FROM expiry")
    assert c.post("/api/expiry/del", data={"id": eid}).get_json()["ok"] is True
    assert db.scalar("SELECT COUNT(*) FROM expiry") == 0


def test_delete_missing_returns_404():
    c = _setup(0)
    assert c.post("/api/expiry/del", data={"id": 999}).status_code == 404


# ---------------------------------------------------------------- 刷新
def test_refresh_writes_date_and_error(monkeypatch=None):
    """网络层用假的：一条成功、一条失败，结果都要落到库里."""
    c = _setup(0)
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO expiry(kind,name,expire,renew,note,provider,last_err,updated_at)
               VALUES('domain','ok.com','',0,'','','',?)""",
            (db.now(),),
        )
        conn.execute(
            """INSERT INTO expiry(kind,name,expire,renew,note,provider,last_err,updated_at)
               VALUES('ssl','bad.com','',0,'','','',?)""",
            (db.now(),),
        )

    calls = {"ok.com": ("2027-08-13", ""), "bad.com": ("", "证书已过期")}
    orig_domain = expiry.rdap.domain_expiry
    orig_ssl = expiry.sslcert.ssl_expiry
    expiry.rdap.domain_expiry = lambda d, timeout=12.0: calls[d]
    expiry.sslcert.ssl_expiry = lambda h, p=443, timeout=8.0: ("", "握手失败")
    try:
        stats = expiry.refresh()
    finally:
        expiry.rdap.domain_expiry = orig_domain
        expiry.sslcert.ssl_expiry = orig_ssl

    assert stats == {"total": 2, "ok": 1, "fail": 1}
    assert db.scalar("SELECT expire FROM expiry WHERE name='ok.com'") == "2027-08-13"
    assert db.scalar("SELECT last_err FROM expiry WHERE name='bad.com'") == "握手失败"


def test_refresh_endpoint_returns_json():
    c = _setup(0)
    resp = c.post("/api/expiry/refresh")
    assert resp.get_json()["ok"] is True


# ---------------------------------------------------------------- 云厂商
def test_provider_add_rejects_bad_json():
    c = _setup(0)
    resp = c.post("/api/expiry/provider/add", data={"kind": "custom", "name": "x", "cfg": "not json"})
    assert resp.status_code == 400
    assert "JSON" in resp.get_json()["error"]


def test_provider_add_and_toggle():
    c = _setup(0)
    resp = c.post(
        "/api/expiry/provider/add",
        data={"kind": "custom", "name": "我的接口",
              "cfg": '{"url":"https://example.com/api","items_path":"data.items"}'},
    )
    payload = resp.get_json()
    assert payload["ok"] is True
    pid = payload["id"]
    assert db.scalar("SELECT COUNT(*) FROM provider") == 1

    assert c.post("/api/expiry/provider/toggle", data={"id": pid}).get_json()["enabled"] is False


def test_provider_del_removes_its_expiries():
    c = _setup(0)
    pid = c.post(
        "/api/expiry/provider/add",
        data={"kind": "custom", "name": "x", "cfg": '{"url":"https://e.com"}'},
    ).get_json()["id"]
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO expiry(kind,name,expire,renew,note,provider,last_err,updated_at)
               VALUES('provider','实例A','2027-01-01',0,'',?,'',?)""",
            (pid, db.now()),
        )
    c.post("/api/expiry/provider/del", data={"id": pid})
    assert db.scalar("SELECT COUNT(*) FROM expiry") == 0
    assert db.scalar("SELECT COUNT(*) FROM provider") == 0
