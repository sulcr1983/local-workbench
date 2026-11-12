"""到期采集的汇总、分级与落库.

四条来源：
- domain  → RDAP / 自建 WHOIS（wb/expiry/rdap.py）
- ssl     → 443 证书（wb/expiry/sslcert.py）
- provider→ 云厂商 SDK（wb/expiry/providers/*）
- manual  → 用户手填，不采集

纪律：所有网络调用都在事务外；落库只做毫秒级 UPDATE。
"""

from __future__ import annotations

import importlib
from datetime import date, datetime

from .. import db
from . import rdap, sslcert

# 5 档：过期 / 7 天内 / 15 天内 / 30 天内 / 正常
LEVELS = (
    (7, "7 天内", "err"),
    (15, "15 天内", "warn"),
    (30, "30 天内", "yellow"),
    (10**9, "正常", "ok"),
)

KIND_LABEL = {"domain": "域名", "ssl": "证书", "provider": "云资源", "manual": "手动"}
PROVIDER_MODULES = {
    "tencent": "wb.expiry.providers.tencent",
    "aliyun": "wb.expiry.providers.aliyun",
    "huawei": "wb.expiry.providers.huawei",
    "custom": "wb.expiry.providers.custom",
}
PROVIDER_KINDS = {
    "tencent": "腾讯云",
    "aliyun": "阿里云",
    "huawei": "华为云",
    "custom": "自定义 API",
}


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def days_left(expire: str | None) -> int | None:
    d = parse_date(expire)
    if d is None:
        return None
    return (d - date.today()).days


def level(days: int | None) -> tuple[str, str]:
    """剩余天数 → (文案, 徽章样式)；没日期算「未采集」."""
    if days is None:
        return "未采集", "mut"
    if days < 0:
        return f"已过期 {abs(days)} 天", "err"
    for limit, label, cls in LEVELS:
        if days <= limit:
            return label, cls
    return "正常", "ok"


def sort_key(row) -> tuple:
    d = days_left(row["expire"])
    return (d is None, d if d is not None else 10**9)


# ------------------------------------------------------------------ 采集
def _collect_one(row) -> tuple[str, str]:
    """(expire, err)；只做网络，不碰库."""
    kind = row["kind"]
    if kind == "domain":
        return rdap.domain_expiry(row["name"])
    if kind == "ssl":
        host = row["name"]
        port = 443
        if ":" in host:
            host, port_text = host.rsplit(":", 1)
            try:
                port = int(port_text)
            except ValueError:
                port = 443
        return sslcert.ssl_expiry(host, port)
    return "", ""


def refresh(only_ids: list[int] | None = None) -> dict:
    """刷新域名与证书；返回统计。云厂商由 sync_providers 单独跑."""
    sql = "SELECT * FROM expiry WHERE kind IN ('domain','ssl')"
    args: tuple = ()
    if only_ids:
        marks = ", ".join("?" for _ in only_ids)
        sql += f" AND id IN ({marks})"
        args = tuple(only_ids)
    rows = db.query(sql, args)

    ok = fail = 0
    updates: list[tuple] = []
    for row in rows:  # 网络阶段：全在事务外
        expire, err = _collect_one(row)
        if expire:
            ok += 1
            updates.append((expire, "", db.now(), row["id"]))
        else:
            fail += 1
            updates.append((row["expire"], err or "采集失败，原因未知", db.now(), row["id"]))

    if updates:
        with db.tx() as c:
            for item in updates:
                c.execute("UPDATE expiry SET expire=?, last_err=?, updated_at=? WHERE id=?", item)
    return {"total": len(rows), "ok": ok, "fail": fail}


def _load_provider_module(kind: str):
    path = PROVIDER_MODULES.get(kind)
    if not path:
        raise ValueError(f"未知的云厂商类型：{kind}")
    return importlib.import_module(path)


def sync_providers() -> dict:
    """逐个 provider 拉实例，按 (provider, name) 合并进 expiry 表."""
    import json

    provs = db.query("SELECT * FROM provider WHERE enabled=1")
    total = created = updated = 0
    errors: list[str] = []

    for prow in provs:
        try:
            mod = _load_provider_module(prow["kind"])
            cfg = json.loads(prow["cfg"] or "{}")
            items = mod.fetch(cfg)
        except Exception as err:  # noqa: BLE001 - 一家失败不影响其他家
            errors.append(f"{prow['name']}：{err}")
            continue

        total += len(items)
        existing = {
            r["name"]: r["id"]
            for r in db.query(
                "SELECT id, name FROM expiry WHERE kind='provider' AND provider=?", (prow["id"],)
            )
        }
        with db.tx() as c:
            for item in items:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                expire = str(item.get("expire") or "")[:10]
                renew = 1 if item.get("renew") else 0
                note = str(item.get("note") or "")[:200]
                if name in existing:
                    c.execute(
                        "UPDATE expiry SET expire=?, renew=?, note=?, last_err='', updated_at=? WHERE id=?",
                        (expire, renew, note, db.now(), existing[name]),
                    )
                    updated += 1
                else:
                    c.execute(
                        """INSERT INTO expiry(kind, name, expire, renew, note, provider, last_err, updated_at)
                           VALUES('provider',?,?,?,?,?,'',?)""",
                        (name, expire, renew, note, prow["id"], db.now()),
                    )
                    created += 1
    return {
        "providers": len(provs),
        "instances": total,
        "created": created,
        "updated": updated,
        "errors": errors,
    }


def refresh_all() -> dict:
    a = refresh()
    b = sync_providers()
    return {"domain_ssl": a, "providers": b}
