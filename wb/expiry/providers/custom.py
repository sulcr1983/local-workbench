"""自定义 API 到期采集 + 四家 provider 共用的小工具。

cfg = {
    "url":         "https://api.example.com/expiries",
    "method":      "GET",              # 可选，默认 GET
    "headers":     {"Authorization": "Bearer ..."},   # 可选
    "items_path":  "data.items",       # 空字符串 = 根节点
    "name_path":   "name",
    "expire_path": "expire_time",
    "renew_path":  "auto_renew",
}

`_dig` 支持点号路径 + list 下标（`data.0.items`）。

除 `ProviderError` 外，日期/布尔归一化与密钥脱敏也放在这里：T12 只允许新建四个
provider 文件，不再单独拆一个公共模块，腾讯/阿里/华为都从这里 import。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlparse

KIND = "custom"
LABEL = "自定义 API"
TIMEOUT = 12

_TRUE_WORDS = {"1", "true", "yes", "on", "y", "是", "自动"}


class ProviderError(Exception):
    """provider 失败。message 直接上页面，必须是中文，且不得包含任何密钥。"""


# ------------------------------------------------------------------ 共用工具
def _scrub(text, *secrets) -> str:
    """把密钥从错误文本里抹掉；错误文本也不该无限长."""
    out = str(text)
    for s in secrets:
        s = "" if s is None else str(s)
        if len(s) >= 3:  # 太短的串替换了会把正常文字一起吞掉
            out = out.replace(s, "***")
    return out[:300]


def _to_date(value) -> str:
    """各种日期写法 → YYYY-MM-DD；认不出返回空串（不抛）."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 1e12:  # 毫秒
                ts /= 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return ""

    text = str(value).strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not m:
        m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        datetime(y, mo, d)
    except ValueError:
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_WORDS


def _regions(cfg: dict, default: tuple[str, ...]) -> tuple[list[str], bool]:
    """(区域列表, 是否用了默认值)。没填就默认一个，由调用方在 note 里说清楚."""
    raw = cfg.get("regions") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").replace("，", " ").split()
    regions = [str(r).strip() for r in raw if str(r).strip()]
    if not regions:
        return list(default), True
    return regions, False


def _dig(obj, path: str):
    """按点号路径取值；路径为空返回原对象，取不到返回 None（不抛）."""
    if not path:
        return obj
    cur = obj
    for seg in str(path).split("."):
        seg = seg.strip()
        if seg == "":
            return None
        if isinstance(cur, list):
            if not seg.lstrip("-").isdigit():
                return None
            idx = int(seg)
            if not -len(cur) <= idx < len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        else:
            return None
    return cur


def _secrets_of(cfg: dict) -> list[str]:
    """自定义 API 的密钥面：请求头值 + URL 查询串里的值."""
    out = [str(v) for v in (cfg.get("headers") or {}).values()]
    try:
        out.extend(v for _, v in parse_qsl(urlparse(str(cfg.get("url") or "")).query))
    except ValueError:
        pass
    return out


# ------------------------------------------------------------------ 采集
def fetch(cfg: dict) -> list[dict]:
    """拉自定义 API 并映射成统一结构；任何一步失败抛 ProviderError."""
    url = str(cfg.get("url") or "").strip()
    if not url:
        raise ProviderError("自定义 API 未填写 URL")

    headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
    secrets = _secrets_of(cfg)
    method = str(cfg.get("method") or "GET").strip().upper()

    try:
        import requests

        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        else:
            resp = requests.request(method, url, headers=headers, timeout=TIMEOUT)
    except Exception as err:  # noqa: BLE001 - 网络层什么都可能抛，一律转中文
        raise ProviderError(f"自定义 API 请求失败：{_scrub(err, *secrets)}") from None

    if resp.status_code != 200:
        raise ProviderError(f"自定义 API 返回 HTTP {resp.status_code}，请检查地址或鉴权")

    text = resp.text or ""
    try:
        data = json.loads(text)
    except ValueError:
        raise ProviderError(f"自定义 API 返回的不是 JSON：{_scrub(text[:120], *secrets)}") from None

    items_path = cfg.get("items_path") or ""
    raw_items = _dig(data, items_path)
    if not isinstance(raw_items, list):
        raise ProviderError(
            f"自定义 API 解析失败：路径 {items_path or '(根节点)'} 取到的不是列表"
        )

    name_path = cfg.get("name_path") or "name"
    expire_path = cfg.get("expire_path") or "expire_time"
    renew_path = cfg.get("renew_path") or "auto_renew"

    out: list[dict] = []
    for i, raw in enumerate(raw_items, 1):
        if not isinstance(raw, dict):
            raise ProviderError(f"自定义 API 解析失败：第 {i} 条不是对象")

        name = _dig(raw, name_path)
        if name is None or str(name).strip() == "":
            raise ProviderError(f"自定义 API 解析失败：第 {i} 条缺少名称（路径 {name_path}）")

        raw_expire = _dig(raw, expire_path)
        if raw_expire is None or str(raw_expire).strip() == "":
            raise ProviderError(
                f"自定义 API 解析失败：第 {i} 条缺少到期时间（路径 {expire_path}）"
            )

        expire = _to_date(raw_expire)
        if not expire:
            raise ProviderError(
                f"自定义 API 解析失败：第 {i} 条到期时间无法识别"
                f"（{_scrub(raw_expire, *secrets)}）"
            )

        renew = _to_bool(_dig(raw, renew_path))
        out.append({
            "name": str(name).strip()[:120],
            "expire": expire,
            "renew": renew,
            "note": "自动续费" if renew else "未自动续费",
        })
    return out


def preview(cfg: dict, limit: int = 3) -> list[dict]:
    """「测试解析」按钮：只返回前 N 条，解析失败照常抛可读错误."""
    try:
        n = max(0, int(limit))
    except (TypeError, ValueError):
        n = 3
    return fetch(cfg)[:n]
