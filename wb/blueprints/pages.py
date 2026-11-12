"""首页：库存总览.

原型里这一页是「一屏看清手上有什么」：四个数、临期、需要注意的、最近动静、
服务器在线情况、最近操作。左侧偏「要处理」，右侧偏「在看什么」，和原型一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, redirect, render_template, url_for

from .. import db
from .settings import load_ui

bp = Blueprint("pages", __name__)


def _needs_setup() -> bool:
    """一次都没配过（没根目录、没服务器、也没点过完成）才拉去向导."""
    if db.get_meta("onboarding_done") == "1":
        return False
    empty = db.scalar("SELECT COUNT(*) FROM root", default=0) == 0 and \
        db.scalar("SELECT COUNT(*) FROM server", default=0) == 0
    return bool(empty)


def _stats() -> dict:
    rows = db.query("SELECT active FROM project")
    return {
        "total": len(rows),
        "active": sum(1 for r in rows if r["active"] == "活跃"),
        "slow": sum(1 for r in rows if r["active"] in ("放缓", "停滞")),
        "dead": sum(1 for r in rows if r["active"] == "可能废弃"),
    }


def _expiring(days: int = 30) -> list[dict]:
    limit = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.query(
        """SELECT * FROM expiry
           WHERE expire IS NOT NULL AND expire <= ?
           ORDER BY expire ASC LIMIT 6""",
        (limit,),
    )
    today = datetime.now().date()
    out = []
    for r in rows:
        try:
            d = datetime.strptime(r["expire"][:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        left = (d - today).days
        out.append(
            {
                "name": r["name"],
                "kind": r["kind"],
                "expire": r["expire"][:10],
                "label": f"已过期 {abs(left)} 天" if left < 0 else f"{left} 天",
                "cls": "err" if left <= 7 else ("warn" if left <= 15 else "yellow"),
            }
        )
    return out


def _attention() -> list[dict]:
    """未提交最多的几个项目 —— 最容易「忘了推」的地方."""
    rows = db.query(
        """SELECT name_auto, user_name, dirname, dirty_n, unpushed_n, last_commit
           FROM project WHERE dirty_n > 0 ORDER BY dirty_n DESC LIMIT 6"""
    )
    return [
        {
            "name": r["user_name"] or r["name_auto"] or r["dirname"],
            "dirty_n": r["dirty_n"],
            "unpushed_n": r["unpushed_n"],
            "last": (r["last_commit"] or "")[:10],
        }
        for r in rows
    ]


def _recent(limit: int = 6) -> list[dict]:
    rows = db.query(
        """SELECT name_auto, user_name, dirname, last_commit FROM project
           WHERE last_commit IS NOT NULL ORDER BY last_commit DESC LIMIT ?""",
        (limit,),
    )
    now = datetime.now()
    out = []
    for r in rows:
        try:
            days = (now - datetime.strptime(r["last_commit"][:19], "%Y-%m-%d %H:%M:%S")).days
        except (TypeError, ValueError):
            days = 999
        label = "今天" if days <= 0 else ("昨天" if days == 1 else
                                         (f"{days} 天前" if days < 30 else f"约 {days // 30} 个月前"))
        out.append({"name": r["user_name"] or r["name_auto"] or r["dirname"], "when": label})
    return out


def _servers() -> list[dict]:
    rows = db.query("SELECT * FROM server ORDER BY name")
    latest = {
        r["server_id"]: r
        for r in db.query("SELECT * FROM metric WHERE id IN (SELECT MAX(id) FROM metric GROUP BY server_id)")
    }
    out = []
    for r in rows:
        m = latest.get(r["id"])
        online = bool(m and m["online"])
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "online": online,
                "note": (m["hostname"] if online and m["hostname"] else "") or r["note"] or "",
            }
        )
    return out[:6]


def _audit(limit: int = 5) -> list[dict]:
    rows = db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
    return [{"ts": r["ts"][5:16], "action": r["action"], "detail": r["detail"] or ""} for r in rows]


@bp.get("/")
def overview():
    if _needs_setup():
        return redirect(url_for("welcome.index"))
    if not load_ui().get("show_overview"):
        return redirect(url_for("projects.index"))

    stats = _stats()
    servers = _servers()
    online = sum(1 for s in servers if s["online"])
    expiring = _expiring()
    attention = _attention()

    todo = []
    if attention:
        todo.append(
            {
                "bdg": f"{sum(a['dirty_n'] for a in attention)} 未提交",
                "cls": "err",
                "text": f"{len(attention)} 个项目有未提交改动",
                "href": url_for("projects.index", flt="dirty"),
                "act": "去处理 →",
            }
        )
    offline = [s for s in servers if not s["online"]]
    if offline:
        todo.append(
            {
                "bdg": f"{len(offline)} 台离线",
                "cls": "warn",
                "text": "、".join(s["name"] for s in offline[:3]),
                "href": url_for("servers.index", flt="offline"),
                "act": "看详情 →",
            }
        )
    if expiring:
        todo.append(
            {
                "bdg": f"{len(expiring)} 项临期",
                "cls": "info",
                "text": "、".join(e["name"] for e in expiring[:3]),
                "href": url_for("expiries.index"),
                "act": "去续费 →",
            }
        )

    return render_template(
        "overview.html",
        page_title="库存总览",
        page_code="OVERVIEW / 001",
        crumb="库存总览",
        active="overview",
        stats=stats,
        expiring=expiring,
        attention=attention,
        recent=_recent(),
        servers=servers,
        online=online,
        todo=todo,
        audits=_audit(),
        counts={"projects": stats["total"], "servers": len(servers)},
        rail=(
            {
                "label": "服务器在线",
                "value": f"{online} / {len(servers)}",
                "hint": "点「刷新全部」拉一次实时状态",
                "pct": round(online * 100 / len(servers)) if servers else None,
            }
            if servers
            else None
        ),
    )
