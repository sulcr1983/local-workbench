"""到期页：域名、证书、云资源、手动项.

T13 范围：清单 + 5 档分级 + 分页 + 手动项增删
另含云厂商配置与通知配置的接口（T12 / T14 的落点）。
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from .. import db, expiry, notify
from ..paging import paginate

bp = Blueprint("expiries", __name__)

PER_PAGE = 10
FLTS = {"all": "全部", "urgent": "30 天内", "expired": "已过期", "manual": "手动项"}


def _rows() -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM expiry ORDER BY id DESC"):
        days = expiry.days_left(r["expire"])
        label, kind_cls = expiry.level(days)
        out.append(
            {
                "id": r["id"],
                "kind": r["kind"],
                "kind_label": expiry.KIND_LABEL.get(r["kind"], r["kind"]),
                "name": r["name"],
                "expire": r["expire"] or "",
                "days": days,
                "level_label": label,
                "level_cls": kind_cls,
                "renew": bool(r["renew"]),
                "note": r["note"] or "",
                "provider": r["provider"] or "",
                "last_err": r["last_err"] or "",
                "updated_at": r["updated_at"],
            }
        )
    out.sort(key=lambda i: (i["days"] is None, i["days"] if i["days"] is not None else 10**9))
    return out


@bp.get("/expiries")
def index():
    rows = _rows()
    flt = request.args.get("flt") or "all"
    if flt == "urgent":
        rows = [r for r in rows if r["days"] is not None and r["days"] <= 30]
    elif flt == "expired":
        rows = [r for r in rows if r["days"] is not None and r["days"] < 0]
    elif flt == "manual":
        rows = [r for r in rows if r["kind"] == "manual"]
    elif flt not in FLTS:
        flt = "all"

    all_rows = _rows()
    stats = {
        "expired": sum(1 for r in all_rows if r["days"] is not None and r["days"] < 0),
        "d7": sum(1 for r in all_rows if r["days"] is not None and 0 <= r["days"] <= 7),
        "d30": sum(1 for r in all_rows if r["days"] is not None and 7 < r["days"] <= 30),
        "total": len(all_rows),
    }

    chunk, page = paginate(rows, request.args.get("page"), PER_PAGE)

    def mk_url(n: int) -> str:
        return url_for("expiries.index", flt=flt, page=n)

    providers = db.query("SELECT * FROM provider ORDER BY id")
    cfg = notify.notify_cfg()

    return render_template(
        "expiries.html",
        page_title="到期提醒",
        page_code="EXPIRIES / 004",
        crumb="到期提醒",
        active="expiries",
        rows=chunk,
        page=page,
        total_all=len(rows),
        stats=stats,
        flt=flt,
        flts=FLTS,
        mk_url=mk_url,
        providers=providers,
        provider_kinds=expiry.PROVIDER_KINDS,
        cfg=cfg,
        counts={"expiries": stats["expired"] + stats["d7"]},
    )


# ---------------------------------------------------------------- 手动项
@bp.post("/api/expiry/add")
def api_add():
    kind = (request.form.get("kind") or "manual").strip()
    name = (request.form.get("name") or "").strip()
    expire = (request.form.get("expire") or "").strip()[:10]
    note = (request.form.get("note") or "").strip()[:200]
    if not name:
        return jsonify({"ok": False, "error": "名称不能为空"}), 400
    # 名称原样进列表首列，不截断会把表格撑破（.wb-pnm 无 ellipsis），跟项目名保持同一口径
    if len(name) > 120:
        return jsonify({"ok": False, "error": "名称最长 120 个字"}), 400
    if kind not in ("manual", "domain", "ssl"):
        return jsonify({"ok": False, "error": f"不支持的类型：{kind}"}), 400
    if expire and expiry.parse_date(expire) is None:
        return jsonify({"ok": False, "error": "日期要写成 2027-05-05 这种格式"}), 400

    with db.tx() as c:
        c.execute(
            """INSERT INTO expiry(kind, name, expire, renew, note, provider, last_err, updated_at)
               VALUES(?,?,?,?,?,'','',?)""",
            (kind, name, expire, 1 if request.form.get("renew") else 0, note, db.now()),
        )
    db.audit("添加到期项", f"{name} · {expire or '未填日期'}")
    return jsonify({"ok": True})


@bp.post("/api/expiry/del")
def api_del():
    eid = request.form.get("id")
    row = db.query_one("SELECT name FROM expiry WHERE id=?", (eid,))
    if row is None:
        return jsonify({"ok": False, "error": "这一条已经不在了"}), 404
    db.execute("DELETE FROM expiry WHERE id=?", (eid,))
    db.audit("删除到期项", row["name"])
    return jsonify({"ok": True})


@bp.post("/api/expiry/refresh")
def api_refresh():
    stats = expiry.refresh_all()
    ds = stats["domain_ssl"]
    pv = stats["providers"]
    db.audit("刷新到期", f"域名证书 {ds['ok']}/{ds['total']} · 云实例 {pv['instances']}")
    if request.form.get("back"):
        return redirect(url_for("expiries.index"))
    return jsonify({"ok": True, **stats})


# ---------------------------------------------------------------- 云厂商
@bp.post("/api/expiry/provider/add")
def api_provider_add():
    kind = (request.form.get("kind") or "").strip()
    name = (request.form.get("name") or "").strip()
    raw = (request.form.get("cfg") or "").strip() or "{}"
    if kind not in expiry.PROVIDER_KINDS:
        return jsonify({"ok": False, "error": f"不支持的厂商类型：{kind}"}), 400
    if not name:
        return jsonify({"ok": False, "error": "给这组配置起个名字"}), 400
    try:
        cfg = json.loads(raw)
    except ValueError as err:
        return jsonify({"ok": False, "error": f"配置不是合法 JSON：{err}"}), 400
    if not isinstance(cfg, dict):
        return jsonify({"ok": False, "error": "配置最外层要是 {…}"}), 400

    pid = request.form.get("id")
    with db.tx() as c:
        if pid:
            c.execute(
                "UPDATE provider SET kind=?, name=?, cfg=? WHERE id=?", (kind, name, json.dumps(cfg, ensure_ascii=False), pid)
            )
        else:
            pid = "p-" + os.urandom(4).hex()
            c.execute(
                "INSERT INTO provider(id, kind, name, enabled, cfg) VALUES(?,?,?,1,?)",
                (pid, kind, name, json.dumps(cfg, ensure_ascii=False)),
            )
    db.audit("保存云厂商配置", f"{name} · {expiry.PROVIDER_KINDS[kind]}")
    return jsonify({"ok": True, "id": pid})


@bp.post("/api/expiry/provider/test")
def api_provider_test():
    """测试解析：只取前 3 条预览，不入库."""
    kind = (request.form.get("kind") or "").strip()
    raw = (request.form.get("cfg") or "").strip() or "{}"
    if kind not in expiry.PROVIDER_KINDS:
        return jsonify({"ok": False, "error": f"不支持的厂商类型：{kind}"}), 400
    try:
        cfg = json.loads(raw)
        mod = expiry._load_provider_module(kind)
        items = mod.fetch(cfg)
    except Exception as err:  # noqa: BLE001 - 直接把原因给用户
        return jsonify({"ok": False, "error": str(err)})

    return jsonify({"ok": True, "count": len(items), "preview": items[:3]})


@bp.post("/api/expiry/provider/del")
def api_provider_del():
    pid = request.form.get("id")
    row = db.query_one("SELECT name FROM provider WHERE id=?", (pid,))
    if row is None:
        return jsonify({"ok": False, "error": "这组配置已经不在了"}), 404
    with db.tx() as c:
        c.execute("DELETE FROM expiry WHERE kind='provider' AND provider=?", (pid,))
        c.execute("DELETE FROM provider WHERE id=?", (pid,))
    db.audit("删除云厂商配置", row["name"])
    return jsonify({"ok": True})


@bp.post("/api/expiry/provider/toggle")
def api_provider_toggle():
    pid = request.form.get("id")
    row = db.query_one("SELECT enabled, name FROM provider WHERE id=?", (pid,))
    if row is None:
        return jsonify({"ok": False, "error": "这组配置已经不在了"}), 404
    new = 0 if row["enabled"] else 1
    db.execute("UPDATE provider SET enabled=? WHERE id=?", (new, pid))
    db.audit("切换云厂商", f"{row['name']} → {'启用' if new else '停用'}")
    return jsonify({"ok": True, "enabled": bool(new)})


# ---------------------------------------------------------------- 通知
@bp.post("/api/expiry/notify")
def api_notify_save():
    fields = (
        "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "mail_to",
        "webhook_type", "webhook_url", "webhook_secret",
    )
    cfg = notify.notify_cfg()
    for key in fields:
        if key in request.form:
            cfg[key] = request.form.get(key) or ""
    cfg["smtp_tls"] = bool(request.form.get("smtp_tls"))

    with db.tx() as c:
        c.execute(
            "INSERT INTO notify_cfg(id, cfg) VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET cfg=excluded.cfg",
            (json.dumps(cfg, ensure_ascii=False),),
        )
    db.audit("保存通知配置", f"webhook={cfg.get('webhook_type')} mail={bool(cfg.get('smtp_host'))}")
    return jsonify({"ok": True})


@bp.post("/api/expiry/notify/test")
def api_notify_test():
    cfg = notify.notify_cfg()
    result = notify.push("【测试】Supersu 工作台", "这是一条测试通知，收到就说明渠道通了。", cfg=cfg)
    return jsonify({"ok": result["ok"], **result})
