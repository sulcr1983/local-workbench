"""443 证书到期：读 notAfter（T11）.

只做一件事：连上去、拿证书、读到期日。
校验失败不抛异常，翻译成「证书已过期 / 域名不匹配 / 自签名证书」；
翻译完成后再用「不校验」的方式连一次，能读到日期就一起返回 ——
这样 UI 上既能提示原因，也能显示"到底哪天过期"。
"""

from __future__ import annotations

import re
import socket
import ssl
from datetime import datetime, timezone

LAST_ERR = ""  # 最近一次失败原因，T13 汇总时可写入 expiry.last_err

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://")
_HOST_OK_RE = re.compile(r"^[a-z0-9\u4e00-\u9fff][a-z0-9\u4e00-\u9fff.\-]*$")


def normalize_host(raw: str) -> str:
    """小写、去协议、去路径、去端口、去 www. —— 与 rdap 那边保持同一套规矩."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    s = _SCHEME_RE.sub("", s)
    s = re.split(r"[/?#]", s, maxsplit=1)[0]
    s = s.rsplit("@", 1)[-1]
    s = s.split(":", 1)[0]
    s = s.strip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s or not _HOST_OK_RE.match(s) or ".." in s:
        return ""
    return s


def _handshake(host: str, port: int, timeout: float, verify: bool) -> dict:
    """完成一次 TLS 握手并返回证书字典；失败时原样抛出，由调用方翻译."""
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
    raw = None
    tls = None
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            raw.settimeout(timeout)
        except OSError:
            pass
        tls = ctx.wrap_socket(raw, server_hostname=host)
        return tls.getpeercert() or {}
    finally:
        for obj in (tls, raw):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:  # noqa: BLE001 - 关不上也不该影响结果
                pass


def _not_after(cert: dict | None) -> str:
    if not cert:
        return ""
    raw = (cert.get("notAfter") or "").strip()
    if not raw:
        return ""
    try:
        ts = ssl.cert_time_to_seconds(raw)
    except (ValueError, TypeError, OverflowError):
        from .rdap import to_iso_date  # 兜底：证书里的时间格式意外时按通用日期解析

        return to_iso_date(raw)
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


def _verify_reason(err: ssl.SSLCertVerificationError) -> str:
    msg = (getattr(err, "verify_message", "") or str(err) or "").lower()
    if "expired" in msg:
        return "证书已过期"
    if "hostname mismatch" in msg or "not valid for" in msg or "doesn't match" in msg:
        return "域名不匹配：证书上的名字和这个域名对不上"
    if "self-signed" in msg or "self signed" in msg:
        return "自签名证书：浏览器不会信任它"
    if "unable to get local issuer" in msg or "unable to get issuer" in msg:
        return "证书链不完整：本机缺少中间证书"
    if "revoked" in msg:
        return "证书已被吊销"
    return f"证书校验失败：{msg[:120] or '未知原因'}"


def ssl_expiry(host: str, port: int = 443, timeout: float = 8.0) -> tuple[str, str]:
    """返回 (YYYY-MM-DD 或 "", 失败原因或 "")；调用方不处理异常.

    校验失败时若还能读出 notAfter，会返回 `(日期, 原因)` —— 两个字段可以同时非空。
    """
    global LAST_ERR

    h = normalize_host(host)
    if not h:
        LAST_ERR = "域名为空或含非法字符"
        return "", "域名为空或格式不对"

    try:
        p = int(port)
    except (TypeError, ValueError):
        p = 443
    if not (1 <= p <= 65535):
        p = 443

    try:
        t = float(timeout)
    except (TypeError, ValueError):
        t = 8.0
    if t <= 0:
        t = 8.0

    try:
        cert = _handshake(h, p, t, verify=True)
    except ssl.SSLCertVerificationError as err:
        reason = _verify_reason(err)
        expired_cert = None
        try:
            expired_cert = _handshake(h, p, t, verify=False)
        except Exception:  # noqa: BLE001 - 二次连接只是为了补日期，失败就算了
            expired_cert = None
        got = _not_after(expired_cert)
        LAST_ERR = f"{reason}（{h}:{p}）"
        return got, reason
    except (socket.timeout, TimeoutError):
        LAST_ERR = f"连接 {h}:{p} 超时（{t:g}s）"
        return "", f"连接超时（{t:g}s）：{h}:{p} 没有响应，可能被防火墙挡了"
    except socket.gaierror:
        LAST_ERR = f"域名解析失败：{h}"
        return "", f"域名解析失败：找不到 {h}"
    except ssl.SSLError as err:
        detail = (str(err) or "未知原因").strip()
        LAST_ERR = f"TLS 握手失败：{detail}"
        return "", f"TLS 握手失败：{detail}（可能只支持老版本协议，或者对方不是 HTTPS）"
    except OSError as err:
        LAST_ERR = f"连不上 {h}:{p}：{err}"
        return "", f"连接失败：{h}:{p} 连不上（{err}）"
    except Exception as err:  # noqa: BLE001 - 这个函数不许往外抛
        LAST_ERR = f"读取证书失败：{err}"
        return "", f"读取证书失败：{err}"

    got = _not_after(cert)
    if not got:
        LAST_ERR = f"{h}:{p} 的证书里没有 notAfter"
        return "", "证书里没有 notAfter 字段，读不到到期日"
    LAST_ERR = ""
    return got, ""
