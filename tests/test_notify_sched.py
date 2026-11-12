"""T14 通知与调度验收：钉钉加签、payload 形状、错误码透传、24h 去重、两个 job."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import tempfile
import urllib.parse
from datetime import date, timedelta

from wb import create_app, db, notify, scheduler


def _setup():
    root = tempfile.mkdtemp(prefix="wb-ntf-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


# ---------------------------------------------------------------- 加签
def test_dingtalk_signed_url_has_ts_and_sign():
    url = notify.dingtalk_signed_url("https://oapi.dingtalk.com/robot/send?access_token=abc", "SECxxx")
    assert "timestamp=" in url and "sign=" in url
    assert url.startswith("https://oapi.dingtalk.com/robot/send?access_token=abc&")

    ts = url.split("timestamp=")[1].split("&")[0]
    sign = url.split("sign=")[1]
    expected_raw = hmac.new(
        b"SECxxx", f"{ts}\nSECxxx".encode("utf-8"), hashlib.sha256
    ).digest()
    expected = urllib.parse.quote_plus(base64.b64encode(expected_raw))
    assert sign == expected, "签名算法必须是 hmac_sha256(secret, ts\\nsecret) 再 base64+urlencode"


def test_dingtalk_without_secret_keeps_url():
    url = "https://x/y?a=1"
    assert notify.dingtalk_signed_url(url, "") == url


# ---------------------------------------------------------------- payload
def test_payload_shapes():
    for kind in ("dingtalk", "wecom"):
        p = notify._payload(kind, "标题", "正文")
        assert p["msgtype"] == "text"
        assert "标题" in p["text"]["content"] and "正文" in p["text"]["content"]

    p2 = notify._payload("custom", "标题", "正文")
    assert p2 == {"title": "标题", "content": "正文"}


# ---------------------------------------------------------------- 发送
class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def test_webhook_success():
    _setup()
    import wb.notify as mod

    mod.requests.post = lambda *a, **k: _Resp(200, {"errcode": 0, "errmsg": "ok"})
    ok, detail = notify.send_webhook({"webhook_type": "dingtalk", "webhook_url": "https://x"}, "t", "c")
    assert ok is True


def test_webhook_error_code_passthrough():
    _setup()
    import wb.notify as mod

    mod.requests.post = lambda *a, **k: _Resp(200, {"errcode": 310000, "errmsg": "sign not match"})
    ok, detail = notify.send_webhook(
        {"webhook_type": "dingtalk", "webhook_url": "https://x", "webhook_secret": "s"}, "t", "c"
    )
    assert ok is False
    assert "310000" in detail and "sign not match" in detail, f"必须原样带回对方的错误码：{detail}"


def test_webhook_http_error():
    _setup()
    import wb.notify as mod

    mod.requests.post = lambda *a, **k: _Resp(502, None, "bad gateway")
    ok, detail = notify.send_webhook({"webhook_type": "wecom", "webhook_url": "https://x"}, "t", "c")
    assert ok is False and "502" in detail


def test_webhook_missing_url():
    _setup()
    ok, detail = notify.send_webhook({"webhook_type": "custom"}, "t", "c")
    assert ok is False and "Webhook" in detail


# ---------------------------------------------------------------- 去重
def test_dedup_within_24h():
    _setup()
    import wb.notify as mod

    mod.requests.post = lambda *a, **k: _Resp(200, {"errcode": 0})
    cfg = {"webhook_type": "dingtalk", "webhook_url": "https://x"}

    first = notify.push("告警", "内存 95%", dedup_key="alert:server:1", cfg=cfg)
    second = notify.push("告警", "内存 95%", dedup_key="alert:server:1", cfg=cfg)

    assert first["ok"] is True and not first.get("skipped")
    assert second.get("skipped") is True, "24h 内同 key 不该重复推"


def test_no_channel_configured():
    _setup()
    result = notify.push("t", "c", cfg={})
    assert result["ok"] is False
    assert "没有配置" in result["detail"]


# ---------------------------------------------------------------- 调度
def test_job_monitor_pushes_over_threshold():
    _setup()
    with db.tx() as c:
        c.execute(
            """INSERT INTO server(name, host, created_at, updated_at, alert_mem)
               VALUES('机器1','1.1.1.1',?,?,85)""",
            (db.now(), db.now()),
        )
        c.execute(
            """INSERT INTO metric(server_id, ts, online, mem_pct, disk_pct, load1)
               VALUES(1,?,1,95.0,10.0,0.5)""",
            (db.now(),),
        )

    calls = []
    import wb.scheduler as sched

    orig = sched.notify.push
    sched.notify.push = lambda title, content, dedup_key="", cfg=None: (
        calls.append(dedup_key) or {"ok": True, "skipped": False}
    )
    orig_refresh = sched.monitor.refresh
    orig_prune = sched.monitor.prune
    sched.monitor.refresh = lambda: {"total": 1}
    sched.monitor.prune = lambda: {"removed": 0}
    try:
        result = sched.job_monitor()
    finally:
        sched.notify.push = orig
        sched.monitor.refresh = orig_refresh
        sched.monitor.prune = orig_prune

    assert calls == ["alert:server:1"], calls
    assert result["pushed"] == 1


def test_job_expiry_pushes_within_7_days():
    _setup()
    soon = (date.today() + timedelta(days=3)).isoformat()
    with db.tx() as c:
        c.execute(
            """INSERT INTO expiry(kind,name,expire,renew,note,provider,last_err,updated_at)
               VALUES('manual','快到期',?,0,'','','',?)""",
            (soon, db.now()),
        )

    calls = []
    import wb.scheduler as sched

    orig_push = sched.notify.push
    orig_refresh = sched.expiry.refresh_all
    sched.notify.push = lambda title, content, dedup_key="", cfg=None: (
        calls.append(dedup_key) or {"ok": True, "skipped": False}
    )
    sched.expiry.refresh_all = lambda: {"domain_ssl": {}, "providers": {}}
    try:
        result = sched.job_expiry()
    finally:
        sched.notify.push = orig_push
        sched.expiry.refresh_all = orig_refresh

    assert len(calls) == 1 and calls[0].startswith("expiry:")
    assert result["pushed"] == 1


def test_scheduler_start_and_state():
    _setup()
    sched = scheduler.start(monitor_minutes=60, expiry_hours=6)
    try:
        ids = {j["id"] for j in scheduler.jobs_state()}
        assert ids == {"wb-monitor", "wb-project-scan", "wb-expiry"}
        assert scheduler.running() is True
    finally:
        scheduler.stop()
    assert scheduler.running() is False
