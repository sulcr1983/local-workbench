"""通知：SMTP 邮件 + Webhook（钉钉 / 企微 / 自定义）.

要点（TECH_SPEC §7.4.1）：
- 钉钉加签：hmac_sha256(secret, f"{ts}\n{secret}") → base64 → urlencode，拼到 URL 上
- 失败必须回传对方的原始 errcode + errmsg，不能只说「发送失败」
- 同一告警 24h 内不重复推（notify_log 表去重）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import smtplib
import time
import urllib.parse
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

from . import config, db

TIMEOUT = 10
DEDUP_HOURS = 24

WEBHOOK_TYPES = {"dingtalk": "钉钉", "wecom": "企业微信", "custom": "自定义"}


# ------------------------------------------------------------------ 去重
def _seen(key: str) -> bool:
    row = db.query_one("SELECT ts FROM notify_log WHERE key=?", (key,))
    if row is None:
        return False
    try:
        prev = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return datetime.now() - prev < timedelta(hours=DEDUP_HOURS)


def _mark(key: str) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO notify_log(key, ts) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts",
            (key, db.now()),
        )


# ------------------------------------------------------------------ 钉钉
def dingtalk_signed_url(url: str, secret: str) -> str:
    """加签：timestamp 毫秒 + HMAC-SHA256(secret, "ts\\nsecret")，base64 后 urlencode."""
    if not secret:
        return url
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={ts}&sign={sign}"


def _payload(kind: str, title: str, content: str) -> dict:
    if kind in ("dingtalk", "wecom"):
        return {"msgtype": "text", "text": {"content": f"{title}\n{content}"}}
    return {"title": title, "content": content}


def send_webhook(cfg: dict, title: str, content: str) -> tuple[bool, str]:
    """返回 (是否成功, 给人看的结果). 失败时带上对方原始 errcode/errmsg."""
    kind = (cfg.get("webhook_type") or "dingtalk").strip()
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        return False, "还没有配置 Webhook 地址"

    if kind == "dingtalk":
        url = dingtalk_signed_url(url, (cfg.get("webhook_secret") or "").strip())

    try:
        resp = requests.post(url, json=_payload(kind, title, content), timeout=TIMEOUT)
    except requests.RequestException as err:
        return False, f"发不出去：{type(err).__name__} · {err}"

    if resp.status_code >= 400:
        return False, f"对方返回 HTTP {resp.status_code}：{resp.text[:200]}"

    # 钉钉/企微都是 200 + 业务码
    try:
        data = resp.json()
    except ValueError:
        return True, f"已发送（对方返回的不是 JSON，HTTP {resp.status_code}）"

    if isinstance(data, dict):
        code = data.get("errcode", data.get("code", 0))
        if str(code) not in ("0", "None", "none"):
            msg = data.get("errmsg") or data.get("message") or ""
            return False, f"对方拒收：errcode={code} · {msg}"
    return True, "已发送"


# ------------------------------------------------------------------ 邮件
def send_mail(cfg: dict, title: str, content: str) -> tuple[bool, str]:
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        return False, "还没有配置 SMTP 服务器"

    sender = (cfg.get("smtp_user") or "").strip()
    to_addr = (cfg.get("mail_to") or "").strip()
    if not to_addr:
        return False, "还没有填收件人"

    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr(("Supersu 工作台", sender or host))
    msg["To"] = to_addr

    port = int(cfg.get("smtp_port") or 465)
    use_ssl = bool(cfg.get("smtp_tls", True)) and port == 465
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            if cfg.get("smtp_tls"):
                server.starttls()
        with server:
            if sender:
                server.login(sender, cfg.get("smtp_pass") or "")
            server.sendmail(sender or host, [a.strip() for a in to_addr.split(",") if a.strip()], msg.as_string())
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP 认证失败：账号或授权码不对（QQ/163 要用授权码，不是登录密码）"
    except smtplib.SMTPException as err:
        return False, f"SMTP 协议错误：{err}"
    except OSError as err:
        return False, f"连不上邮件服务器：{err}"
    return True, "已发送"


# ------------------------------------------------------------------ 统一入口
def notify_cfg() -> dict:
    row = db.query_one("SELECT cfg FROM notify_cfg WHERE id=1")
    data = dict(config.DEFAULT_NOTIFY)
    if row and row["cfg"]:
        try:
            data.update(json.loads(row["cfg"]))
        except ValueError:
            pass
    return data


def push(title: str, content: str, dedup_key: str = "", cfg: dict | None = None) -> dict:
    """按配置推一次；dedup_key 非空时 24h 内同 key 不再推."""
    cfg = cfg or notify_cfg()
    if dedup_key and _seen(dedup_key):
        return {"ok": True, "skipped": True, "detail": "24 小时内已经推过同一条"}

    results = {}
    ok_any = False

    if (cfg.get("webhook_url") or "").strip():
        ok, detail = send_webhook(cfg, title, content)
        results["webhook"] = {"ok": ok, "detail": detail}
        ok_any = ok_any or ok

    if (cfg.get("smtp_host") or "").strip():
        ok, detail = send_mail(cfg, title, content)
        results["mail"] = {"ok": ok, "detail": detail}
        ok_any = ok_any or ok

    if not results:
        return {"ok": False, "skipped": False, "detail": "没有配置任何通知渠道", "results": {}}

    if ok_any and dedup_key:
        _mark(dedup_key)
    return {"ok": ok_any, "skipped": False, "results": results}
