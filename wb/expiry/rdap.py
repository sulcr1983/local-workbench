"""域名到期采集：RDAP → WHOIS → 手动兜底（T11 / TECH_SPEC §7.4）.

对外接口：
    domain_expiry(domain, timeout=12.0) -> (YYYY-MM-DD 或 "", 失败原因或 "")
    whois_server_for(tld) -> str
    parse_whois_expiry(text) -> str
    normalize_domain(raw) -> str

纪律（照 `scan.py` / `ssh.py` 的做法）：
- 这个模块永远不往外抛异常，任何失败都转成一句中文原因。
- 404 必须分两种语义：IANA 引导文件里该 TLD 有 RDAP → 404 是「未注册」；
  没分配 → 404 只代表这条链不通，继续走 WHOIS。
- 总耗时压到 25s 以内：RDAP 9s + IANA 5s + WHOIS 5s×2 是硬上限，
  实际每个阶段都按剩余预算二次收缩。
"""

from __future__ import annotations

import re
import socket
import threading
import time
from datetime import date

import requests

# ---------------------------------------------------------------- 常量
RDAP_URL = "https://rdap.org/domain/{domain}"
IANA_RDAP_URL = "https://data.iana.org/rdap/dns.json"

RDAP_TIMEOUT = 9.0
IANA_TIMEOUT = 5.0
WHOIS_TIMEOUT = 5.0
TOTAL_BUDGET = 25.0

IANA_TTL = 24 * 3600  # 引导文件一天拉一次就够，别每个域名都请求

WHOIS_PORT = 43
MAX_WHOIS_BYTES = 256 * 1024

USER_AGENT = "supersu-workbench/1.0 (local expiry checker)"

# 最近一次失败的具体原因（T13 汇总时读它写进 expiry.last_err）。
# 返回值里的第二项是给用户看的一句话，细节留在这儿。
LAST_ERR = ""

# ---------------------------------------------------------------- 静态 WHOIS 表
# 常见后缀打底，命中就不必再问 IANA，省一跳。
WHOIS_SERVERS = {
    "com": "whois.verisign-grs.com",
    "net": "whois.verisign-grs.com",
    "org": "whois.pir.org",
    "cn": "whois.cnnic.cn",
    "io": "whois.nic.io",
    "ai": "whois.nic.ai",
    "xyz": "whois.nic.xyz",
    "top": "whois.nic.top",
    "dev": "whois.nic.google",
    "app": "whois.nic.google",
    "info": "whois.nic.info",
    "biz": "whois.nic.biz",
    "co": "whois.nic.co",
    "me": "whois.nic.me",
    "tv": "tvwhois.verisign-grs.com",
    "cc": "ccwhois.verisign-grs.com",
    "name": "whois.nic.name",
    "pro": "whois.nic.pro",
    "mobi": "whois.nic.mobi",
    "asia": "whois.nic.asia",
    "online": "whois.nic.online",
    "site": "whois.nic.site",
    "tech": "whois.nic.tech",
    "store": "whois.nic.store",
    "shop": "whois.nic.shop",
    "club": "whois.nic.club",
    "vip": "whois.nic.vip",
    "work": "whois.nic.work",
    "ltd": "whois.nic.ltd",
    "group": "whois.nic.group",
    "cloud": "whois.nic.cloud",
    "email": "whois.nic.email",
    "live": "whois.nic.live",
    "fun": "whois.nic.fun",
    "icu": "whois.nic.icu",
    "uk": "whois.nic.uk",
    "de": "whois.denic.de",
    "fr": "whois.nic.fr",
    "eu": "whois.eu",
    "jp": "whois.jprs.jp",
    "hk": "whois.hkirc.hk",
    "tw": "whois.twnic.net.tw",
    "kr": "whois.kr",
    "ru": "whois.tcinet.ru",
    "au": "whois.auda.org.au",
    "ca": "whois.cira.ca",
    "in": "whois.registry.in",
    "us": "whois.nic.us",
    "sg": "whois.sgnic.sg",
    "nz": "whois.srs.net.nz",
    "br": "whois.registro.br",
    "it": "whois.nic.it",
    "nl": "whois.domain-registry.nl",
    "se": "whois.iis.se",
    "no": "whois.norid.no",
    "dk": "whois.dk-hostmaster.dk",
    "fi": "whois.fi",
    "pl": "whois.dns.pl",
    "ch": "whois.nic.ch",
    "at": "whois.nic.at",
    "be": "whois.dns.be",
    "es": "whois.nic.es",
    "pt": "whois.dns.pt",
    "cz": "whois.nic.cz",
    "gr": "whois.nic.gr",
    "mx": "whois.mx",
    "cl": "whois.nic.cl",
}

# 二级后缀（com.cn 这类），查的时候先试具体后缀再退回一级。
WHOIS_TWO_LEVEL = {
    "com.cn": "whois.cnnic.cn",
    "net.cn": "whois.cnnic.cn",
    "org.cn": "whois.cnnic.cn",
    "gov.cn": "whois.cnnic.cn",
    "edu.cn": "whois.cnnic.cn",
    "ac.cn": "whois.cnnic.cn",
    "co.uk": "whois.nic.uk",
    "org.uk": "whois.nic.uk",
    "me.uk": "whois.nic.uk",
    "ac.uk": "whois.nic.uk",
    "gov.uk": "whois.nic.uk",
    "ltd.uk": "whois.nic.uk",
    "com.hk": "whois.hkirc.hk",
    "org.hk": "whois.hkirc.hk",
    "net.hk": "whois.hkirc.hk",
    "edu.hk": "whois.hkirc.hk",
    "com.tw": "whois.twnic.net.tw",
    "org.tw": "whois.twnic.net.tw",
    "net.tw": "whois.twnic.net.tw",
    "co.jp": "whois.jprs.jp",
    "ne.jp": "whois.jprs.jp",
    "or.jp": "whois.jprs.jp",
    "ac.jp": "whois.jprs.jp",
    "go.jp": "whois.jprs.jp",
    "com.au": "whois.auda.org.au",
    "net.au": "whois.auda.org.au",
    "org.au": "whois.auda.org.au",
    "edu.au": "whois.auda.org.au",
    "co.nz": "whois.srs.net.nz",
    "net.nz": "whois.srs.net.nz",
    "org.nz": "whois.srs.net.nz",
    "com.br": "whois.registro.br",
    "co.in": "whois.registry.in",
    "net.in": "whois.registry.in",
    "org.in": "whois.registry.in",
}


# ---------------------------------------------------------------- 域名规范化
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://")
_HOST_OK_RE = re.compile(r"^[a-z0-9\u4e00-\u9fff][a-z0-9\u4e00-\u9fff.\-]*$")


def normalize_domain(raw: str) -> str:
    """小写、去协议、去路径、去端口、去 www. —— 只留主机名."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    s = _SCHEME_RE.sub("", s)
    s = re.split(r"[/?#]", s, maxsplit=1)[0]
    s = s.rsplit("@", 1)[-1]          # user:pass@host
    s = s.split(":", 1)[0]            # host:8443
    s = s.strip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s or not _HOST_OK_RE.match(s):
        return ""
    if ".." in s:
        return ""
    return s


def _tld_candidates(domain: str) -> list[str]:
    """["example", "com"] → ["com"]；["example", "com", "cn"] → ["com.cn", "cn"].

    只有已知二级后缀才两段一起试，避免把 "example.zz" 当成后缀 "example.zz"。
    """
    parts = [p for p in domain.split(".") if p]
    if len(parts) < 2:
        return []
    if len(parts) >= 3:
        last2 = ".".join(parts[-2:])
        if last2 in WHOIS_TWO_LEVEL:
            return [last2, parts[-1]]
    return [parts[-1]]


# ---------------------------------------------------------------- WHOIS 服务器
def whois_server_for(tld: str) -> str:
    """按后缀给 WHOIS 服务器；查不到返回 ""（调用方再走 IANA 两跳）."""
    t = (tld or "").strip().lower().lstrip(".")
    if not t:
        return ""
    if t in WHOIS_SERVERS:
        return WHOIS_SERVERS[t]
    if t in WHOIS_TWO_LEVEL:
        return WHOIS_TWO_LEVEL[t]
    if "." in t:  # 传进来的是完整域名或更长的后缀，退回最后一段
        return whois_server_for(t.rsplit(".", 1)[-1])
    return ""


# ---------------------------------------------------------------- IANA 引导文件
_iana_lock = threading.Lock()
_iana_cache: dict = {"ts": 0.0, "map": None}


def _clear_iana_cache() -> None:
    """测试用：把 24h 缓存清掉，保证每个用例都从干净的桩开始."""
    with _iana_lock:
        _iana_cache["ts"] = 0.0
        _iana_cache["map"] = None


def _iana_map(timeout: float) -> dict[str, bool] | None:
    """IANA 的 RDAP 分配表 {tld: True}；拉不到返回 None（不代表"没有"）."""
    now = time.time()
    cached = _iana_cache.get("map")
    if cached is not None and now - float(_iana_cache.get("ts") or 0.0) < IANA_TTL:
        return cached
    try:
        resp = requests.get(
            IANA_RDAP_URL,
            timeout=max(1.0, timeout),
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        services = data.get("services") or []
    except Exception:  # noqa: BLE001 - 网络/JSON 全都算"拿不到"
        return None

    table: dict[str, bool] = {}
    for svc in services:
        try:
            tlds = svc[0]
            urls = svc[1]
        except (IndexError, TypeError):
            continue
        if not urls:
            continue
        for t in tlds or []:
            if isinstance(t, str) and t.strip():
                table[t.strip().lower()] = True

    with _iana_lock:
        _iana_cache["ts"] = now
        _iana_cache["map"] = table
    return table


def _tld_has_rdap(tld: str, timeout: float) -> bool | None:
    table = _iana_map(timeout)
    if table is None:
        return None
    return tld.strip().lower().lstrip(".") in table


# ---------------------------------------------------------------- 日期解析
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ISO_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DMY_RE = re.compile(r"(\d{1,2})[-\s/]([A-Za-z]{3,9})[-\s/](\d{4})")
_MDY_RE = re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})")
_COMPACT_RE = re.compile(r"\b(\d{4})(\d{2})(\d{2})\b")


def _mk_date(year: int, month: int, day: int) -> str:
    if not (1990 <= year <= 2100):
        return ""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def to_iso_date(value: str) -> str:
    """把 WHOIS/RDAP 里各种日期写法统一成 YYYY-MM-DD；认不出返回 ""."""
    s = (value or "").strip()
    if not s:
        return ""

    m = _ISO_RE.search(s)
    if m:
        return _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _DMY_RE.search(s)  # 13-aug-2027 / 05 May 2027
    if m:
        month = _MONTHS.get(m.group(2)[:4].lower()) or _MONTHS.get(m.group(2)[:3].lower())
        if month:
            return _mk_date(int(m.group(3)), month, int(m.group(1)))

    m = _MDY_RE.search(s)  # Aug 13 2027 / August 13, 2027
    if m:
        month = _MONTHS.get(m.group(1)[:4].lower()) or _MONTHS.get(m.group(1)[:3].lower())
        if month:
            return _mk_date(int(m.group(3)), month, int(m.group(2)))

    m = _COMPACT_RE.search(s)  # 20270813
    if m:
        return _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    return ""


# ---------------------------------------------------------------- WHOIS 文本解析
# 一行一个 "键: 值"，键名按语义打分，分数最高的那条就是到期日。
_KEY_LINE_RE = re.compile(
    r"^[ \t]*([A-Za-z\u4e00-\u9fff][\w \u4e00-\u9fff.()/\-]{0,60}?)[ \t]*[:：][ \t]*([^\r\n]+?)[ \t]*$",
    re.MULTILINE,
)

_EXACT_KEY_SCORE = {
    "registry expiry date": 100,
    "registry expiration date": 99,
    "registrar registration expiration date": 96,
    "registrar registration expiry date": 95,
    "expiration time": 92,
    "域名过期日期": 91,
    "到期时间": 91,
    "domain expiration date": 90,
    "expiry date": 86,
    "expiration date": 84,
    "expires on": 82,
    "expires": 80,
    "paid-till": 78,
    "renewal date": 70,
    "valid until": 68,
}

_CONTAINS_KEY_SCORE = (
    ("registry expiry", 100),
    ("registrar registration expiration", 96),
    ("expiration time", 92),
    ("registry expiration", 90),
    ("expiry date", 86),
    ("expiration date", 84),
    ("paid-till", 78),
    ("expires", 74),
    ("renewal", 62),
    ("valid until", 60),
    ("过期", 88),
    ("到期", 88),
)


def _squash_key(key: str) -> str:
    return re.sub(r"\s+", " ", (key or "").strip().lower())


def _key_score(key: str) -> int:
    k = _squash_key(key)
    if not k:
        return 0
    score = _EXACT_KEY_SCORE.get(k)
    if score is not None:
        return score
    for frag, s in _CONTAINS_KEY_SCORE:
        if frag in k:
            return s - 1  # 同义变体略低于精确键
    return 0


def parse_whois_expiry(text: str) -> str:
    """从 WHOIS 纯文本里取到期日，失败返回 "".

    实测命中过的写法：
        Registry Expiry Date: 2027-08-13T04:00:00Z        (.com)
        Expiration Time: 2027-05-05 05:38:46              (.cn)
        paid-till: 2027-05-05T00:00:00Z                    (.ru)
        Expiry Date: 13-aug-2027                           (部分 gTLD)
        Registrar Registration Expiration Date: ...        (注册商段)
    """
    if not text:
        return ""

    best_date = ""
    best_score = 0
    best_pos = 1 << 30

    for idx, m in enumerate(_KEY_LINE_RE.finditer(text)):
        score = _key_score(m.group(1))
        if score <= 0:
            continue
        got = to_iso_date(m.group(2))
        if not got:
            continue
        if score > best_score or (score == best_score and idx < best_pos):
            best_date, best_score, best_pos = got, score, idx

    return best_date


# ---------------------------------------------------------------- WHOIS 网络
def _whois_query(server: str, query: str, timeout: float) -> str:
    """连 43 端口问一句；任何问题都返回 ""（不抛）."""
    if not server or not query:
        return ""
    sock = None
    try:
        sock = socket.create_connection((server, WHOIS_PORT), timeout=max(1.0, timeout))
        try:
            sock.settimeout(max(1.0, timeout))
        except OSError:
            pass
        sock.sendall((query + "\r\n").encode("utf-8", errors="ignore"))
        chunks: list[bytes] = []
        total = 0
        while total < MAX_WHOIS_BYTES:
            try:
                buf = sock.recv(4096)
            except socket.timeout:
                break
            if not buf:
                break
            chunks.append(buf)
            total += len(buf)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 连不上/DNS/对端关连接，全算空
        return ""
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


def _iana_whois_server(tld: str, timeout: float) -> str:
    """问 whois.iana.org 拿 "whois:" 行 —— 静态表没覆盖的后缀用这条路."""
    text = _whois_query("whois.iana.org", tld, timeout)
    if not text:
        return ""
    m = re.search(r"^[ \t]*whois[ \t]*[:：][ \t]*(\S+)", text, re.IGNORECASE | re.MULTILINE)
    if not m:
        return ""
    host = m.group(1).strip().strip(".")
    return host if "." in host else ""


def _whois_lookup(domain: str, tlds: list[str], deadline: float) -> tuple[str, str]:
    tried: set[str] = set()
    last_why = ""

    for tld in tlds:
        server = whois_server_for(tld)
        if not server or server in tried:
            continue
        budget = _slice(deadline, WHOIS_TIMEOUT)
        if budget <= 0:
            return "", "WHOIS 查询超出总时长预算"
        tried.add(server)
        text = _whois_query(server, domain, budget)
        if not text:
            last_why = f"连不上 {server}"
            continue
        got = parse_whois_expiry(text)
        if got:
            return got, ""
        last_why = f"{server} 返回的文本里没有到期字段"

    # 静态表没覆盖、或覆盖了但没查出来 → 走 IANA 两跳
    for tld in tlds:
        budget = _slice(deadline, WHOIS_TIMEOUT)
        if budget <= 0:
            return "", last_why or "WHOIS 查询超出总时长预算"
        server = _iana_whois_server(tld, budget)
        if not server or server in tried:
            continue
        budget = _slice(deadline, WHOIS_TIMEOUT)
        if budget <= 0:
            return "", last_why or "WHOIS 查询超出总时长预算"
        tried.add(server)
        text = _whois_query(server, domain, budget)
        if not text:
            last_why = f"连不上 {server}"
            continue
        got = parse_whois_expiry(text)
        if got:
            return got, ""
        last_why = f"{server} 返回的文本里没有到期字段"

    return "", last_why or "WHOIS 没有返回可解析的到期时间"


# ---------------------------------------------------------------- RDAP
def _rdap_lookup(domain: str, timeout: float) -> tuple[str, str, str]:
    """返回 (日期, 状态, 中文原因)；状态用于区分 404 语义."""
    url = RDAP_URL.format(domain=domain)
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"Accept": "application/rdap+json", "User-Agent": USER_AGENT},
        )
    except Exception as err:  # noqa: BLE001 - 超时/DNS/代理，全算网络失败
        return "", "net", f"RDAP 连不上（{_net_reason(err)}）"

    if resp.status_code == 404:
        return "", "not_found", "RDAP 返回 404"
    if resp.status_code != 200:
        return "", "http", f"RDAP 返回 HTTP {resp.status_code}"

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return "", "bad_json", "RDAP 返回的内容不是 JSON"

    events = data.get("events") if isinstance(data, dict) else None
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("eventAction") or "").strip().lower() != "expiration":
            continue
        got = to_iso_date(str(ev.get("eventDate") or ""))
        if got:
            return got, "ok", ""

    return "", "no_expiry", "RDAP 响应里没有 expiration 事件"


def _net_reason(err: Exception) -> str:
    text = str(err) or type(err).__name__
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return "超时"
    if "name or service not known" in low or "getaddrinfo" in low or "nodename" in low:
        return "域名解析失败"
    if "proxy" in low:
        return "代理不可用"
    return text[:80]


def _slice(deadline: float, cap: float) -> float:
    """在总预算内给单步分一个超时；没预算了返回 0."""
    left = deadline - time.monotonic()
    if left < 1.0:
        return 0.0
    return max(1.0, min(cap, left))


# ---------------------------------------------------------------- 主入口
def domain_expiry(domain: str, timeout: float = 12.0) -> tuple[str, str]:
    """返回 (YYYY-MM-DD 或 "", 失败原因或 "")；调用方不处理异常.

    三段按顺序：RDAP → (404 先查 IANA 判语义) → WHOIS → 手动兜底。
    第二项非空时，`LAST_ERR` 里还有更细的技术原因。
    """
    global LAST_ERR

    name = normalize_domain(domain)
    if not name:
        LAST_ERR = "域名为空或含非法字符"
        return "", "域名为空或格式不对"
    if "." not in name:
        LAST_ERR = f"{name} 缺少后缀"
        return "", "域名格式不对，请填 example.com 这种"

    try:
        cap = float(timeout)
    except (TypeError, ValueError):
        cap = 12.0
    if cap <= 0:
        cap = 12.0

    deadline = time.monotonic() + TOTAL_BUDGET
    tlds = _tld_candidates(name)
    tld = tlds[-1]
    notes: list[str] = []

    kind, why = "", ""
    budget = _slice(deadline, min(cap, RDAP_TIMEOUT))
    if budget > 0:
        got, kind, why = _rdap_lookup(name, budget)
        if got:
            LAST_ERR = ""
            return got, ""
    else:
        kind, why = "skip", "总时长预算不够，跳过 RDAP"
    if why:
        notes.append(why)

    if kind == "not_found":
        budget = _slice(deadline, IANA_TIMEOUT)
        has_rdap = _tld_has_rdap(tld, budget) if budget > 0 else None
        if has_rdap is True:
            LAST_ERR = f"{name}：RDAP 404，且 IANA 给 .{tld} 分配了 RDAP 服务，判定为未注册"
            return "", "域名可能未注册"
        if has_rdap is None:
            notes.append("IANA 引导文件拿不到，无法确认 404 是未注册还是该后缀无 RDAP")
        # has_rdap is False → 该后缀没有 RDAP，404 属正常，继续 WHOIS

    got, wwhy = _whois_lookup(name, tlds, deadline)
    if got:
        LAST_ERR = ""
        return got, ""
    if wwhy:
        notes.append(wwhy)

    LAST_ERR = "；".join(n for n in notes if n) or "RDAP 与 WHOIS 都没拿到到期时间"
    return "", "该后缀不支持自动查询，请手动填"
