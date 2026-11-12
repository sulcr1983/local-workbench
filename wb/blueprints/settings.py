"""设置页：界面开关、扫描根目录、活跃度规则、配置导出导入.

T16 范围：
- ui_cfg 单行（id=1）存界面开关与活跃度规则
- root 表的增删（只删配置，不动磁盘）
- config.json 交换格式的导出（脱敏）与导入（按名称 / 路径合并）

约定（TECH_SPEC §6.2）：
- 所有 POST 走 request.form，返回 {"ok": bool, "error": str?}
- 写操作成功即 db.audit()
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, Response, jsonify, render_template, request

from .. import config, db
from ..scan import DEFAULT_RULES

bp = Blueprint("settings", __name__)

# 界面开关：只认这几个键，别的字段一律不写
UI_BOOL_KEYS = ("show_overview", "show_expiries", "browser_alive")

UI_LABELS = {
    "show_overview": "库存总览",
    "show_expiries": "到期提醒",
    "browser_alive": "浏览器常驻",
}

CONFIG_VERSION = 1
EXPORT_FILENAME = "workbench-config.json"


# ------------------------------------------------------------------ 小工具
def _as_bool(value) -> bool:
    """表单里来的可能是 '1' / '0' / 'true'，统一成 bool."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _loads(raw) -> dict:
    """json 字符串 → dict；脏数据不炸，退回空 dict."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ 界面开关
def load_ui() -> dict:
    """界面开关 + 活跃度规则的合并视图。

    config.DEFAULT_UI 打底，库里 ui_cfg 的值盖在上面。
    __init__.py 的上下文处理器调它（签名保持 load_ui() -> dict）。
    """
    data = _loads(db.scalar("SELECT cfg FROM ui_cfg WHERE id=1", default=""))

    ui = dict(config.DEFAULT_UI)
    ui.update(data)
    for key in UI_BOOL_KEYS:
        ui[key] = _as_bool(ui.get(key, False))

    rules = dict(DEFAULT_RULES)
    raw_rules = ui.get("rules")
    if isinstance(raw_rules, dict):
        for key in DEFAULT_RULES:
            if key in raw_rules:
                rules[key] = _as_int(raw_rules[key], DEFAULT_RULES[key])
    ui["rules"] = rules
    ui["scan_scope"] = data.get("scan_scope") or "full"
    return ui


def _save_ui(patch: dict) -> dict:
    """改 ui_cfg：读出全量 → 合并补丁 → 写回（只动单行 id=1）."""
    data = _loads(db.scalar("SELECT cfg FROM ui_cfg WHERE id=1", default=""))
    data.update(patch)
    with db.tx() as c:
        c.execute(
            "INSERT INTO ui_cfg(id, cfg) VALUES(1, ?) "
            "ON CONFLICT(id) DO UPDATE SET cfg=excluded.cfg",
            (json.dumps(data, ensure_ascii=False),),
        )
    return data


def _notify_cfg() -> dict:
    row = db.query_one("SELECT cfg FROM notify_cfg WHERE id=1")
    data = dict(config.DEFAULT_NOTIFY)
    data.update(_loads(row["cfg"] if row else ""))
    return data


# ------------------------------------------------------------------ 页面
@bp.get("/settings")
def index():
    from .. import scheduler

    ui = load_ui()
    roots = db.query("SELECT path, added_at FROM root ORDER BY added_at")
    return render_template(
        "settings.html",
        page_title="设置",
        page_code="SETTINGS / 007",
        crumb="设置",
        active="settings",
        ui=ui,
        rules=ui["rules"],
        roots=roots,
        ui_labels=UI_LABELS,
        jobs=scheduler.jobs_state(),
        sched_on=scheduler.running(),
        counts={},
    )


@bp.post("/api/ui/toggle")
def api_ui_toggle():
    key = (request.form.get("key") or "").strip()
    value = request.form.get("value")
    if key not in UI_BOOL_KEYS:
        return jsonify({"ok": False, "error": f"不支持的开关：{key}"}), 400

    new = _as_bool(value)
    _save_ui({key: new})
    db.audit("修改界面开关", f"{UI_LABELS.get(key, key)} → {'开' if new else '关'}")
    return jsonify({"ok": True, "key": key, "value": new})


# ------------------------------------------------------------------ 活跃度规则
@bp.post("/api/settings/rules")
def api_rules_save():
    active = _as_int(request.form.get("active"), -1)
    slow = _as_int(request.form.get("slow"), -1)
    stale = _as_int(request.form.get("stale"), -1)

    if active < 0 or slow < 0 or stale < 0:
        return jsonify({"ok": False, "error": "天数要填 0 以上的整数"}), 400
    if not active <= slow <= stale:
        return jsonify({"ok": False, "error": "三个阈值要从小到大：活跃 ≤ 放缓 ≤ 停滞"}), 400

    rules = {"active": active, "slow": slow, "stale": stale}
    _save_ui({"rules": rules})
    db.audit("修改活跃度规则", f"活跃 {active} 天 / 放缓 {slow} 天 / 停滞 {stale} 天")
    return jsonify({"ok": True, "rules": rules})


# ------------------------------------------------------------------ 根目录
@bp.post("/api/root/add")
def api_root_add():
    path = (request.form.get("path") or "").strip().strip('"')
    if not path:
        return jsonify({"ok": False, "error": "先填一个文件夹路径"}), 400

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": f"这个目录不存在：{path}"}), 400

    with db.tx() as c:
        c.execute(
            "INSERT INTO root(path, added_at) VALUES(?,?) ON CONFLICT(path) DO NOTHING",
            (path, db.now()),
        )
    db.audit("添加根目录", path)
    return jsonify({"ok": True, "path": path})


@bp.post("/api/root/del")
def api_root_del():
    """只删配置行，磁盘上的目录一个字节都不动."""
    path = (request.form.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少根目录路径"}), 400

    row = db.query_one("SELECT path FROM root WHERE path=?", (path,))
    if row is None:
        return jsonify({"ok": False, "error": "这个根目录已经不在了"}), 404

    db.execute("DELETE FROM root WHERE path=?", (path,))
    db.audit("删除根目录", f"{path}（磁盘文件未动）")
    return jsonify({"ok": True})


# ------------------------------------------------------------------ 导出
# 服务器导出字段白名单：只列**确实需要交换**的字段，凭据列（secret/passphrase）
# 故意不出现在白名单里 —— 用的是「列出要导出的字段」而不是「查全表再排除敏感字段」，
# 因为后者漏写一条规则就会把新加的敏感列带出去。
SERVER_EXPORT_FIELDS = (
    "id", "name", "host", "port", "user", "auth",
    "jump", "grp", "note",
    "alert_mem", "alert_disk", "alert_load", "enabled", "legacy_kex",
)


def build_export() -> dict:
    """交换格式（TECH_SPEC §5.2）：凭据一律抹成空串，URL 参数里的 token 打码."""
    roots = [r["path"] for r in db.query("SELECT path FROM root ORDER BY added_at")]
    servers = [
        {k: r[k] for k in SERVER_EXPORT_FIELDS}
        for r in db.query(
            "SELECT " + ", ".join(SERVER_EXPORT_FIELDS) + " FROM server ORDER BY grp, name"
        )
    ]

    projects: dict[str, dict] = {}
    for r in db.query(
        "SELECT path, user_name, user_status, user_note, user_pinned FROM project"
    ):
        if not (r["user_name"] or r["user_status"] or r["user_note"] or r["user_pinned"]):
            continue  # 没手工改过的项目不写进交换格式
        projects[r["path"]] = {
            "name": r["user_name"] or "",
            "status": r["user_status"] or "",
            "note": r["user_note"] or "",
            "pinned": bool(r["user_pinned"]),
        }

    payload = {
        "version": CONFIG_VERSION,
        "roots": roots,
        "ui": load_ui(),
        "servers": servers,
        "projects": projects,
        "notify": _notify_cfg(),
        "_redacted": list(config.SECRET_KEYS),
    }
    out = config.masked_config(payload)
    # masked_config 不认 webhook_url 里的 key/access_token —— 再来一层 URL 掩码
    return config.deep_mask_urls(out)


@bp.get("/api/config/export")
def export_config():
    payload = build_export()
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    db.audit(
        "导出配置",
        f"{len(payload['servers'])} 台服务器 · {len(payload['projects'])} 个项目 · 凭据已脱敏",
    )
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{EXPORT_FILENAME}"'},
    )


# ------------------------------------------------------------------ 导入
def _import_servers(items) -> int:
    """按名称合并：同名更新（空密码不覆盖），不同名新增。返回处理条数."""
    if not isinstance(items, list):
        return 0

    count = 0
    with db.tx() as c:
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            host = str(raw.get("host") or "").strip()
            if not name or not host:
                continue

            alerts = raw.get("alerts") if isinstance(raw.get("alerts"), dict) else {}
            params = (
                host,
                _as_int(raw.get("port"), 22),
                str(raw.get("user") or "root").strip(),
                str(raw.get("auth") or "key_path").strip(),
                str(raw.get("secret") or ""),
                str(raw.get("passphrase") or ""),
                str(raw.get("jump") or "").strip(),
                str(raw.get("grp") or raw.get("group") or "").strip(),
                str(raw.get("note") or "").strip(),
                _as_int(raw.get("alert_mem", alerts.get("mem")), 85),
                _as_int(raw.get("alert_disk", alerts.get("disk")), 90),
                _as_float(raw.get("alert_load", alerts.get("load")), 4.0),
                1 if _as_bool(raw.get("enabled", True)) else 0,
                1 if _as_bool(raw.get("legacy_kex", False)) else 0,
            )
            ts = db.now()

            exists = c.execute("SELECT id FROM server WHERE name=?", (name,)).fetchone()
            if exists:
                # 已存的密码不被空值覆盖（COALESCE(NULLIF(...)) 保住原值）
                c.execute(
                    """UPDATE server SET host=?, port=?, user=?, auth=?,
                           secret=COALESCE(NULLIF(?, ''), secret),
                           passphrase=COALESCE(NULLIF(?, ''), passphrase),
                           jump=?, grp=?, note=?, alert_mem=?, alert_disk=?,
                           alert_load=?, enabled=?, legacy_kex=?, updated_at=?
                       WHERE name=?""",
                    (*params, ts, name),
                )
            else:
                c.execute(
                    """INSERT INTO server(name, host, port, user, auth, secret, passphrase,
                           jump, grp, note, alert_mem, alert_disk, alert_load, enabled,
                           legacy_kex, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (name, *params[:9], *params[9:], ts, ts),
                )
            count += 1
    return count


def _import_projects(items) -> int:
    """按 path 合并手工覆盖：user_name / user_status / user_note / user_pinned."""
    if not isinstance(items, dict):
        return 0

    count = 0
    with db.tx() as c:
        for raw_path, raw in items.items():
            if not isinstance(raw, dict):
                continue
            path = str(raw_path or "").strip()
            if not path:
                continue

            name = str(raw.get("name") or raw.get("user_name") or "").strip()
            status = str(raw.get("status") or raw.get("user_status") or "").strip()
            note = str(raw.get("note") or raw.get("user_note") or "").strip()
            pinned = 1 if _as_bool(raw.get("pinned", raw.get("user_pinned", False))) else 0
            ts = db.now()

            exists = c.execute("SELECT path FROM project WHERE path=?", (path,)).fetchone()
            if exists:
                c.execute(
                    """UPDATE project SET user_name=?, user_status=?, user_note=?,
                           user_pinned=? WHERE path=?""",
                    (name or None, status or None, note or None, pinned, path),
                )
            else:
                # 库里没有这个项目：补一条最小记录，手工覆盖别丢
                c.execute(
                    """INSERT INTO project(path, dirname, user_name, user_status, user_note,
                           user_pinned, first_seen, last_seen)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        path,
                        os.path.basename(path.rstrip("\\/")) or path,
                        name or None,
                        status or None,
                        note or None,
                        pinned,
                        ts,
                        ts,
                    ),
                )
            count += 1
    return count


def _import_roots(items) -> int:
    """根目录只补不存在的；目录不在磁盘上就跳过（不制造假配置）."""
    if not isinstance(items, list):
        return 0

    count = 0
    with db.tx() as c:
        for raw in items:
            path = str(raw or "").strip().strip('"')
            if not path:
                continue
            path = os.path.abspath(path)
            if not os.path.isdir(path):
                continue
            c.execute(
                "INSERT INTO root(path, added_at) VALUES(?,?) ON CONFLICT(path) DO NOTHING",
                (path, db.now()),
            )
            count += 1
    return count


def _import_notify(raw) -> int:
    """通知配置：脱敏过的凭据字段直接跳过，不覆盖已存的口令."""
    if not isinstance(raw, dict):
        return 0

    merged = _notify_cfg()
    for key, value in raw.items():
        if key in config.SECRET_KEYS:
            continue
        merged[key] = value
    with db.tx() as c:
        c.execute(
            "INSERT INTO notify_cfg(id, cfg) VALUES(1, ?) "
            "ON CONFLICT(id) DO UPDATE SET cfg=excluded.cfg",
            (json.dumps(merged, ensure_ascii=False),),
        )
    return 1


@bp.post("/api/config/import")
def import_config():
    # 前端走 FormData 的 payload 字段；脚本/curl 直接发 JSON body 也认。
    # 两者都没有时必须明确报错 —— 以前会静默返回 ok:true + 0/0，
    # 用户看到「导入完成：服务器 0 台 · 项目 0 个」还以为成功了。
    raw = request.form.get("payload") or ""
    if raw.strip():
        try:
            payload = json.loads(raw)
        except ValueError as err:
            return jsonify({"ok": False, "error": f"这段不是合法 JSON：{err}"}), 400
    else:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(
                {"ok": False, "error": "没收到配置文件内容，请重新选择文件后再导入。"}
            ), 400

    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "配置文件最外层要是一个对象 {…}"}), 400

    n_servers = _import_servers(payload.get("servers"))
    n_projects = _import_projects(payload.get("projects"))
    _import_roots(payload.get("roots"))
    _import_notify(payload.get("notify"))

    db.audit("导入配置", f"服务器 {n_servers} 台 · 项目 {n_projects} 个")
    return jsonify({"ok": True, "servers": n_servers, "projects": n_projects})
