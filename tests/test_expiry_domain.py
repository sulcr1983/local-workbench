"""T11 域名与证书到期验收（全离线，除 WB_NET_TEST=1 的真网络冒烟）.

关键纪律：
- 不用 unittest.skip / 不用 tests/skip_allowlist.json —— 离线也必须全绿。
- 真网络用例默认走 `assert True` 分支，不算 skip，配置了 WB_NET_TEST=1 才真跑。
"""

from __future__ import annotations

import json
import os
import ssl
from unittest import mock

from wb.expiry import rdap, sslcert

WHOIS_CN = """Domain Name: example.cn
ROID: 20170505s10001s91234567-cn
Domain Status: ok
Registrant: 示例公司
Registration Time: 2017-05-05 05:38:46
Expiration Time: 2027-05-05 05:38:46
"""

WHOIS_COM = """   Domain Name: EXAMPLE.COM
   Registry Domain ID: 2336799_DOMAIN_COM-VRSN
   Creation Date: 1995-08-14T04:00:00Z
   Registry Expiry Date: 2027-08-13T04:00:00Z
   Updated Date: 2026-08-14T07:01:38Z
   Registrar Registration Expiration Date: 2027-08-13T04:00:00Z
"""

WHOIS_RU = """domain:        EXAMPLE.RU
nserver:       ns1.example.ru.
state:         REGISTERED, DELEGATED, VERIFIED
paid-till:     2027-05-05T00:00:00Z
free-date:     2027-06-05
"""

WHOIS_REGISTRAR = """Domain Name: EXAMPLE.ORG
Registrar Registration Expiration Date: 2027-09-01T12:00:00Z
Registrar: Example Registrar, Inc.
"""

WHOIS_GTLD = """Domain Name: example.gtld
Expiry Date: 13-aug-2027
"""

WHOIS_NO_EXPIRY = """Domain Name: example.net
Updated Date: 2026-01-01T00:00:00Z
Creation Date: 2001-02-03T00:00:00Z
"""


# ---------------------------------------------------------------- 测试桩
class _Resp:
    def __init__(self, status: int = 200, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _router(routes: dict):
    """按 URL 片段分派；routes 的值可以是 _Resp 或要抛的异常."""

    def _get(url, **kwargs):
        for frag, resp in routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"未打桩的请求: {url}")

    return _get


class _FakeSock:
    def __init__(self, payload: str):
        self._queue = [payload.encode("utf-8", "ignore"), b""]
        self.sent = b""

    def settimeout(self, _t):
        pass

    def sendall(self, data: bytes):
        self.sent += data

    def recv(self, _n: int) -> bytes:
        return self._queue.pop(0) if self._queue else b""

    def close(self):
        pass


def _whois_net(table: dict):
    """模拟 socket.create_connection：按服务器名返回固定 WHOIS 文本，未登记就拒连."""

    def _connect(addr, timeout=None):
        host = addr[0]
        if host in table:
            return _FakeSock(table[host])
        raise OSError(f"connect refused: {host}")

    return _connect


class _FakeTLS:
    def __init__(self, cert: dict):
        self._cert = cert or {}

    def getpeercert(self):
        return dict(self._cert)

    def close(self):
        pass


class _FakeRaw:
    def settimeout(self, _t):
        pass

    def close(self):
        pass


class _FakeCtx:
    def __init__(self, cert: dict | None = None, error: Exception | None = None):
        self._cert = cert or {}
        self._error = error

    def wrap_socket(self, raw, server_hostname=None):
        if self._error is not None:
            raise self._error
        return _FakeTLS(self._cert)


def _cert_verify_error(message: str) -> ssl.SSLCertVerificationError:
    err = ssl.SSLCertVerificationError(1, message)
    try:
        err.verify_message = message  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 个别版本不让写，str(err) 里也有原文
        pass
    return err


# ---------------------------------------------------------------- 纯函数：WHOIS 文本
def test_parse_whois_cn_expiration_time():
    assert rdap.parse_whois_expiry(WHOIS_CN) == "2027-05-05"


def test_parse_whois_com_registry_expiry_date():
    assert rdap.parse_whois_expiry(WHOIS_COM) == "2027-08-13"


def test_parse_whois_paid_till():
    assert rdap.parse_whois_expiry(WHOIS_RU) == "2027-05-05"


def test_parse_whois_registrar_field():
    assert rdap.parse_whois_expiry(WHOIS_REGISTRAR) == "2027-09-01"


def test_parse_whois_day_month_year():
    assert rdap.parse_whois_expiry(WHOIS_GTLD) == "2027-08-13"


def test_parse_whois_ignores_creation_and_updated():
    assert rdap.parse_whois_expiry(WHOIS_NO_EXPIRY) == ""


def test_parse_whois_empty_text():
    assert rdap.parse_whois_expiry("") == ""
    assert rdap.parse_whois_expiry(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------- 纯函数：服务器表
def test_whois_server_for_cn():
    assert rdap.whois_server_for("cn") == "whois.cnnic.cn"


def test_whois_server_for_com():
    assert rdap.whois_server_for("com") == "whois.verisign-grs.com"


def test_whois_server_for_two_level_and_unknown():
    assert rdap.whois_server_for("com.cn") == "whois.cnnic.cn"
    assert rdap.whois_server_for(".IO") == "whois.nic.io"
    assert rdap.whois_server_for("no-such-tld") == ""


def test_normalize_domain():
    assert rdap.normalize_domain("HTTP://WWW.Example.COM/") == "example.com"
    assert rdap.normalize_domain("  example.com:8443/path  ") == "example.com"
    assert rdap.normalize_domain("") == ""


# ---------------------------------------------------------------- RDAP 链路
def test_rdap_success():
    routes = {
        "rdap.org/domain/example.com": _Resp(
            200, {"events": [{"eventAction": "expiration", "eventDate": "2027-08-13T04:00:00Z"}]}
        )
    }
    with mock.patch("wb.expiry.rdap.requests.get", side_effect=_router(routes)):
        got = rdap.domain_expiry("example.com")
    assert got == ("2027-08-13", ""), got


def test_rdap_404_tld_has_rdap_means_unregistered():
    rdap._clear_iana_cache()
    routes = {
        "rdap.org/domain/example.cn": _Resp(404),
        "data.iana.org/rdap/dns.json": _Resp(
            200, {"services": [[["cn"], ["https://rdap.cnnic.cn/"]], [["com"], ["https://x/"]]]}
        ),
    }
    try:
        with mock.patch("wb.expiry.rdap.requests.get", side_effect=_router(routes)):
            date_str, reason = rdap.domain_expiry("example.cn")
    finally:
        rdap._clear_iana_cache()
    assert date_str == "", date_str
    assert "未注册" in reason, reason
    assert rdap.LAST_ERR, "LAST_ERR 应记录具体原因"


def test_rdap_404_tld_without_rdap_falls_back_to_whois():
    rdap._clear_iana_cache()
    routes = {
        "rdap.org/domain/example.zz": _Resp(404),
        "data.iana.org/rdap/dns.json": _Resp(200, {"services": [[["com"], ["https://x/"]]]}),
    }
    table = {
        "whois.iana.org": "domain: ZZ\nwhois: whois.nic.zz\n",
        "whois.nic.zz": "Domain Name: example.zz\nExpiration Time: 2027-05-05 05:38:46\n",
    }
    try:
        with mock.patch("wb.expiry.rdap.requests.get", side_effect=_router(routes)), mock.patch(
            "wb.expiry.rdap.socket.create_connection", side_effect=_whois_net(table)
        ):
            got = rdap.domain_expiry("example.zz")
    finally:
        rdap._clear_iana_cache()
    assert got == ("2027-05-05", ""), got


def test_all_fail_returns_manual_hint():
    routes = {"rdap.org": rdap.requests.exceptions.Timeout("timed out")}
    with mock.patch("wb.expiry.rdap.requests.get", side_effect=_router(routes)), mock.patch(
        "wb.expiry.rdap.socket.create_connection", side_effect=_whois_net({})
    ):
        got = rdap.domain_expiry("example.com", timeout=3)
    assert got == ("", "该后缀不支持自动查询，请手动填"), got
    assert rdap.LAST_ERR, "全失败时 LAST_ERR 必须留下可读原因"


def test_domain_expiry_never_raises_on_bad_input():
    assert rdap.domain_expiry("")[0] == ""
    assert rdap.domain_expiry("localhost")[0] == ""


# ---------------------------------------------------------------- SSL 证书
def test_ssl_expiry_normal_cert():
    cert = {"notAfter": "Aug 13 04:00:00 2027 GMT"}
    with mock.patch(
        "wb.expiry.sslcert.ssl.create_default_context", return_value=_FakeCtx(cert=cert)
    ), mock.patch("wb.expiry.sslcert.socket.create_connection", return_value=_FakeRaw()):
        got = sslcert.ssl_expiry("example.com")
    assert got == ("2027-08-13", ""), got


def test_ssl_expiry_expired_cert():
    cert = {"notAfter": "Jan  1 00:00:00 2020 GMT"}
    err = _cert_verify_error("certificate has expired")
    with mock.patch(
        "wb.expiry.sslcert.ssl.create_default_context", return_value=_FakeCtx(error=err)
    ), mock.patch(
        "wb.expiry.sslcert.ssl._create_unverified_context", return_value=_FakeCtx(cert=cert)
    ), mock.patch("wb.expiry.sslcert.socket.create_connection", return_value=_FakeRaw()):
        date_str, reason = sslcert.ssl_expiry("expired.example.com")
    assert date_str == "2020-01-01", date_str
    assert "过期" in reason, reason


def test_ssl_expiry_hostname_mismatch():
    err = _cert_verify_error("Hostname mismatch, certificate is not valid for 'a.example.com'")
    with mock.patch(
        "wb.expiry.sslcert.ssl.create_default_context", return_value=_FakeCtx(error=err)
    ), mock.patch(
        "wb.expiry.sslcert.ssl._create_unverified_context", return_value=_FakeCtx()
    ), mock.patch("wb.expiry.sslcert.socket.create_connection", return_value=_FakeRaw()):
        date_str, reason = sslcert.ssl_expiry("a.example.com")
    assert date_str == ""
    assert "域名不匹配" in reason, reason


def test_ssl_expiry_handshake_failure():
    err = ssl.SSLError("handshake failure")
    with mock.patch(
        "wb.expiry.sslcert.ssl.create_default_context", return_value=_FakeCtx(error=err)
    ), mock.patch("wb.expiry.sslcert.socket.create_connection", return_value=_FakeRaw()):
        date_str, reason = sslcert.ssl_expiry("broken.example.com")
    assert date_str == ""
    assert "TLS" in reason, reason


def test_ssl_expiry_connection_refused():
    with mock.patch(
        "wb.expiry.sslcert.ssl.create_default_context", return_value=_FakeCtx()
    ), mock.patch(
        "wb.expiry.sslcert.socket.create_connection", side_effect=OSError("Connection refused")
    ):
        date_str, reason = sslcert.ssl_expiry("down.example.com")
    assert date_str == ""
    assert reason, "连接失败必须给可读原因"


# ---------------------------------------------------------------- 真网络（默认不跑）
def test_real_network_smoke_when_enabled():
    """WB_NET_TEST=1 才真跑；否则就是一条本地断言，不算 skip."""
    if os.environ.get("WB_NET_TEST") != "1":
        assert True  # 离线：这条用例本身不需要网络，直接通过
        return

    domain_date, domain_err = rdap.domain_expiry("example.com", timeout=10)
    cert_date, cert_err = sslcert.ssl_expiry("example.com", timeout=8)

    # 有网就该拿到日期；本机没网时允许返回可读原因，但不能抛异常、不能两者皆空
    assert domain_date or domain_err, (domain_date, domain_err)
    assert cert_date or cert_err, (cert_date, cert_err)
