"""服务器与云资源：列表、详情、增删改、验证连接.

T9：列表 + 6 指标 + 离线态 + 分页
T10：弹窗表单 + 验证连接 + 编辑/删除
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from .. import bootstrap, db, monitor, ssh
from ..paging import paginate

bp = Blueprint("servers", __name__)

PER_PAGE = 8
AUTHS = {"password": "密码", "key_path": "私钥文件", "key_content": "私钥内容",
         "agent": "SSH Agent", "none": "免认证（用户名即凭据）"}



# 有含义的常用端口先亮出来，剩下的按区间折叠 —— 387 个平铺等于没信息
WELL_KNOWN = {
    "22": "SSH", "80": "HTTP", "443": "HTTPS", "3306": "MySQL", "5432": "PG",
    "6379": "Redis", "27017": "Mongo", "8080": "HTTP-Alt", "8000": "Dev",
    "8443": "HTTPS-Alt", "9200": "ES", "5672": "RabbitMQ", "11211": "Memcached",
    "9090": "Prometheus", "3000": "Node", "5000": "Flask",
}
PORT_BUCKETS = (
    ("系统端口（<1024）", 0, 1024),
    ("常见服务（1024–9999）", 1024, 10000),
    ("高位端口（10000–32767）", 10000, 32768),
    ("动态端口（32768+）", 32768, 65536),
)


def group_ports(ports: list[str], details: list[dict] | None = None) -> dict:
    """387 个端口直接铺开会把页面撑成数字墙。常用的挑出来，其余按区间折叠.

    带 `details`（来自 `ss -p`）时，每个端口会附上「谁在听、对外还是只对本机」。
    没有明细也要能正常出结果 —— 老数据 / 无 root 权限都取不到 users 段。
    """
    by_port: dict[int, list[dict]] = {}
    for d in details or []:
        try:
            by_port.setdefault(int(d["port"]), []).append(d)
        except (KeyError, TypeError, ValueError):
            continue

    def info(n: int) -> dict:
        ds = by_port.get(n) or []
        procs = sorted({d.get("proc") for d in ds if d.get("proc")})
        scopes = {d.get("scope") for d in ds}
        # 只要有一个地址是对外绑的，就算对外可达
        scope = "all" if "all" in scopes else ("local" if scopes else "")
        return {"port": n, "proc": procs, "scope": scope}

    nums = []
    for p in ports or []:
        try:
            nums.append(int(str(p).lstrip(":")))
        except (TypeError, ValueError):
            continue
    common = [n for n in nums if str(n) in WELL_KNOWN]
    rest = [n for n in nums if str(n) not in WELL_KNOWN]

    groups = []
    for label, lo, hi in PORT_BUCKETS:
        items = sorted(n for n in rest if lo <= n < hi)
        if items:
            groups.append({"label": label, "ports": items})

    return {
        "common": [info(n) for n in sorted(common)],
        "labels": WELL_KNOWN,
        "groups": groups,
        "detail": {n: info(n) for n in nums},
        "has_detail": bool(by_port),
        "total": len(nums),
    }


def _port_details(metric_row) -> list[dict]:
    """从 metric.port_json 读出端口明细；老数据没有这一列就返回空."""
    if metric_row is None or "port_json" not in metric_row.keys():
        return []
    raw = metric_row["port_json"]
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _latest_metrics() -> dict[int, object]:
    rows = db.query(
        "SELECT * FROM metric WHERE id IN (SELECT MAX(id) FROM metric GROUP BY server_id)"
    )
    return {r["server_id"]: r for r in rows}


def _card(row, m) -> dict:
    online = bool(m and m["online"])
    mem_pct = m["mem_pct"] if m else None
    disk_pct = m["disk_pct"] if m else None
    alert = online and (
        (mem_pct is not None and mem_pct >= row["alert_mem"])
        or (disk_pct is not None and disk_pct >= row["alert_disk"])
        or (m["load1"] is not None and m["load1"] >= row["alert_load"])
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "host": row["host"],
        "port": row["port"],
        "user": row["user"],
        "grp": row["grp"] or "",
        "note": row["note"] or "",
        "auth": row["auth"],
        "auth_label": AUTHS.get(row["auth"], row["auth"]),
        "online": online,
        "alert": bool(alert),
        "hostname": m["hostname"] if m else "",
        "os_name": (m["os_name"] if m and "os_name" in m.keys() else "") or "",
        "kernel": (m["kernel"] if m and "kernel" in m.keys() else "") or "",
        "uptime": (m["uptime_text"] if m and "uptime_text" in m.keys() else "") or "",
        "load1": m["load1"] if m else None,
        "mem_pct": mem_pct,
        "mem_used": m["mem_used"] if m else None,
        "mem_total": m["mem_total"] if m else None,
        "disk_pct": disk_pct,
        "disk_used": m["disk_used"] if m else "",
        "disk_total": m["disk_total"] if m else "",
        "containers": (m["containers"] or "").split(",") if m and m["containers"] else [],
        "ports": (m["ports"] or "").split(",") if m and m["ports"] else [],
        "error": m["error"] if m else "",
        "ts": m["ts"] if m else "",
    }


def _all_cards() -> list[dict]:
    rows = db.query("SELECT * FROM server ORDER BY grp, name")
    latest = _latest_metrics()
    return [_card(r, latest.get(r["id"])) for r in rows]


@bp.get("/servers")
def index():
    cards = _all_cards()
    flt = request.args.get("flt") or "all"
    if flt == "online":
        cards = [c for c in cards if c["online"]]
    elif flt == "offline":
        cards = [c for c in cards if not c["online"]]
    elif flt == "alert":
        cards = [c for c in cards if c["alert"]]

    online = sum(1 for c in cards if c["online"])
    alerts = [c for c in cards if c["alert"]]
    chunk, page = paginate(cards, request.args.get("srv_page"), PER_PAGE)

    def mk_url(n: int) -> str:
        return url_for("servers.index", flt=flt, srv_page=n)

    return render_template(
        "servers.html",
        page_title="服务器和云资源",
        page_code="SERVERS / 003",
        crumb="服务器和云资源",
        active="servers",
        cards=chunk,
        page=page,
        total_all=len(cards),
        online=online,
        alerts=alerts,
        flt=flt,
        mk_url=mk_url,
        auths=AUTHS,
        counts={"servers": len(cards)},
        # 一台都没有时不显示侧栏底部那块 —— 空数据挂个"0/0"只是噪音
        rail=(
            {
                "label": "服务器在线",
                "value": f"{online} / {len(cards)}",
                "hint": "点「刷新全部」拉一次实时状态",
                "pct": round(online * 100 / len(cards)) if cards else None,
            }
            if cards
            else None
        ),
    )


@bp.post("/servers/refresh")
def refresh_now():
    stats = monitor.refresh()
    db.audit("刷新巡检", f"{stats.get('online', 0)}/{stats.get('total', 0)} 在线")
    return redirect(url_for("servers.index"))


@bp.get("/server/<int:sid>")
def detail(sid: int):
    row = db.query_one("SELECT * FROM server WHERE id=?", (sid,))
    if row is None:
        abort(404)
    latest = db.query_one(
        "SELECT * FROM metric WHERE server_id=? ORDER BY id DESC LIMIT 1", (sid,)
    )
    history = db.query(
        "SELECT ts, load1, mem_pct, disk_pct, online FROM metric WHERE server_id=? ORDER BY id DESC LIMIT 12",
        (sid,),
    )
    card = _card(row, latest)
    projects = db.query(
        """SELECT p.path, p.dirname, p.name_auto, p.user_name FROM project p
           JOIN project_server ps ON ps.project_path = p.path
           WHERE ps.server_id=?""",
        (sid,),
    )
    # 服务器详情页也要能反向勾选项目 —— 不是只能从项目那边挂过来
    # 已勾的排最前面：项目有三十来个，让用户滚半天找自己挂的那几个不像话
    all_projects = db.query(
        """SELECT path, dirname, COALESCE(user_name, name_auto, dirname) AS label,
                  CASE WHEN path IN (SELECT project_path FROM project_server WHERE server_id=?)
                       THEN 0 ELSE 1 END AS pinned
           FROM project WHERE state != 'dropped'
           ORDER BY pinned, label COLLATE NOCASE""",
        (sid,),
    )
    # 链接要用 rel（相对根目录），不是绝对路径 —— 否则 URL 里会出现 %5C 这种东西
    from .projects import rel_of

    rel_map = {p["path"]: rel_of(p["path"]) for p in all_projects}
    projects = [
        {**dict(p), "rel": rel_map.get(p["path"], p["dirname"])}
        for p in projects
    ]
    return render_template(
        "server_detail.html",
        page_title=card["name"],
        page_code="SERVER DETAIL",
        crumb=card["name"],
        active="servers",
        s=card,
        sid=sid,
        history=list(reversed(history)),
        projects=projects,
        all_projects=all_projects,
        linked_paths={r["path"] for r in projects},
        ports=group_ports(card.get("ports") or [], _port_details(latest)),
        counts={},
    )


# ---------------------------------------------------------------- 写入
def _form_server() -> dict:
    return {
        "name": (request.form.get("name") or "").strip(),
        "host": (request.form.get("host") or "").strip(),
        "port": request.form.get("port") or "22",
        "user": (request.form.get("user") or "root").strip(),
        "auth": (request.form.get("auth") or "key_path").strip(),
        "secret": request.form.get("secret") or "",
        "passphrase": request.form.get("passphrase") or "",
        "jump": (request.form.get("jump") or "").strip(),
        "grp": (request.form.get("grp") or "").strip(),
        "note": (request.form.get("note") or "").strip(),
        "alert_mem": request.form.get("alert_mem") or "85",
        "alert_disk": request.form.get("alert_disk") or "90",
        "alert_load": request.form.get("alert_load") or "4.0",
        "legacy_kex": 1 if request.form.get("legacy_kex") else 0,
    }


def _validate(data: dict, is_edit: bool = False) -> str | None:
    if not data["name"]:
        return "服务器名称不能为空"
    if not data["host"]:
        return "主机地址不能为空"
    try:
        port = int(data["port"])
        if not (0 < port < 65536):
            raise ValueError
    except (TypeError, ValueError):
        return "端口要填 1-65535 之间的数字"
    if data["auth"] not in AUTHS:
        return f"不支持的认证方式：{data['auth']}"
    # 编辑时留空表示「不修改已存的凭据」，不能按新增的规则拦
    if not is_edit and data["auth"] in ("password", "key_path", "key_content") and not data["secret"]:
        return "这种认证方式必须填密码/私钥"
    return None


@bp.post("/api/server/test")
def api_server_test():
    """验证连接：只连一次拿 hostname，成功才允许入库."""
    data = _form_server()
    err = _validate(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    cfg = {**data, "port": int(data["port"]), "legacy_kex": bool(data["legacy_kex"])}
    ok, detail = ssh.test_connection(cfg)
    if not ok:
        return jsonify({"ok": False, "error": detail}), 200
    return jsonify({"ok": True, "hostname": detail})


@bp.post("/api/server/save")
def api_server_save():
    data = _form_server()
    sid = request.form.get("id")
    err = _validate(data, is_edit=bool(sid))
    if err:
        return jsonify({"ok": False, "error": err}), 400

    params = (
        data["name"], data["host"], int(data["port"]), data["user"], data["auth"],
        data["secret"], data["passphrase"], data["jump"], data["grp"], data["note"],
        int(float(data["alert_mem"])), int(float(data["alert_disk"])), float(data["alert_load"]),
        data["legacy_kex"],
    )
    ts = db.now()

    try:
        with db.tx() as c:
            if sid:
                # 编辑时留空 = 不修改已存凭据（COALESCE(NULLIF(...)) 保住原值）
                c.execute(
                    """UPDATE server SET name=?, host=?, port=?, user=?, auth=?,
                       secret=COALESCE(NULLIF(?, ''), secret),
                       passphrase=COALESCE(NULLIF(?, ''), passphrase),
                       jump=?, grp=?, note=?, alert_mem=?, alert_disk=?,
                       alert_load=?, legacy_kex=?, updated_at=? WHERE id=?""",
                    (*params, ts, sid),
                )
                action, target = "修改服务器", sid
            else:
                cur = c.execute(
                    """INSERT INTO server(name, host, port, user, auth, secret, passphrase,
                       jump, grp, note, alert_mem, alert_disk, alert_load, legacy_kex,
                       created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*params, ts, ts),
                )
                action, target = "添加服务器", cur.lastrowid
    except Exception as exc:  # noqa: BLE001 - 唯一约束等都要转成可读提示
        msg = str(exc)
        if "UNIQUE" in msg:
            return jsonify({"ok": False, "error": f"已经有叫「{data['name']}」的服务器了，换个名字"}), 400
        return jsonify({"ok": False, "error": f"保存失败：{msg}"}), 400

    db.audit(action, f"{data['name']} · {data['host']}:{data['port']}")
    return jsonify({"ok": True, "id": target})


@bp.post("/api/server/del")
def api_server_del():
    sid = request.form.get("id")
    if not sid:
        return jsonify({"ok": False, "error": "缺少服务器 id"}), 400

    row = db.query_one("SELECT name FROM server WHERE id=?", (sid,))
    if row is None:
        return jsonify({"ok": False, "error": "这台服务器已经不在了"}), 404

    refs = db.scalar("SELECT COUNT(*) FROM project_server WHERE server_id=?", (sid,), default=0)
    # 连不上的死机器往往还挂着几个项目关联 —— 不给强删，人就得先去逐个解绑，
    # 结果就是灰条永远清不掉。force=1 表示「关联一起断，我知道」。
    force = (request.form.get("force") or "") in ("1", "true", "on")
    if refs and not force:
        return jsonify(
            {"ok": False, "error": f"还有 {refs} 个项目关联着这台服务器。删除会一并解除关联。",
             "refs": refs, "need_confirm": True}
        ), 400

    with db.tx() as c:
        c.execute("DELETE FROM project_server WHERE server_id=?", (sid,))
        c.execute("DELETE FROM metric WHERE server_id=?", (sid,))
        c.execute("DELETE FROM server WHERE id=?", (sid,))
    db.audit("删除服务器", f"{row['name']}（解除 {refs} 个项目关联）" if refs else row["name"])
    return jsonify({"ok": True, "unlinked": refs})


@bp.post("/api/server/update")
def api_server_update():
    """单字段更新（阈值、备注、启用开关）."""
    sid = request.form.get("id")
    field = (request.form.get("field") or "").strip()
    value = request.form.get("value")
    allowed = {"alert_mem", "alert_disk", "alert_load", "note", "enabled", "grp"}
    if field not in allowed:
        return jsonify({"ok": False, "error": f"不支持的字段：{field}"}), 400
    if not sid:
        return jsonify({"ok": False, "error": "缺少服务器 id"}), 400

    db.execute(
        f"UPDATE server SET {field}=?, updated_at=? WHERE id=?", (value, db.now(), sid)
    )
    db.audit("修改服务器", f"id={sid} · {field}={value}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 一键发现
@bp.post("/api/servers/discover")
def api_servers_discover():
    """扫本机 + 已配的云账号，把能连的服务器全捞出来.

    系统把能做的做完（扫、探、试连、填 user/port/key），
    剩下「填云厂商 AK/SK」「补密码」这两件只有人能做的事，页面上单独标出来。
    """
    roots = [r["path"] for r in db.query("SELECT path FROM root ORDER BY added_at")]
    try:
        result = bootstrap.gather(roots)
    except Exception as err:  # noqa: BLE001 - 扫描不该把页面搞崩
        return jsonify({"ok": False, "error": f"扫描出错：{str(err).splitlines()[0][:120]}"}), 500

    db.audit(
        "一键发现服务器",
        f"扫到 {result['stats']['total']} 台 · 可直接导入 {result['stats']['ready']} 台",
    )
    return jsonify({"ok": True, **result})


@bp.post("/api/servers/import")
def api_servers_import():
    """批量导入勾选的候选。只收白名单字段，已有 host:port 跳过."""
    raw = request.form.get("items") or ""
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "选择的数据格式不对"}), 400
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "没有勾选任何服务器"}), 400

    result = bootstrap.import_items(items)
    db.audit("批量导入服务器", f"新增 {result['added']} 台 · 跳过 {result['skipped']} 台")
    return jsonify({"ok": True, **result})


@bp.post("/api/server/link")
def api_server_link():
    """服务器详情页里的「这台机器上跑着什么」——按项目勾选/取消.

    与项目详情页的 `/api/project/server` 是同一条关联的两头，
    哪边点都算数，不搞单行道。
    """
    sid = (request.form.get("id") or "").strip()
    path = (request.form.get("path") or "").strip()
    on = (request.form.get("on") or "1") == "1"
    if not sid or not path:
        return jsonify({"ok": False, "error": "缺少服务器或项目"}), 400

    if db.query_one("SELECT id FROM server WHERE id=?", (sid,)) is None:
        return jsonify({"ok": False, "error": "这台服务器已经不在了"}), 404
    proj = db.query_one("SELECT dirname FROM project WHERE path=?", (path,))
    if proj is None:
        return jsonify({"ok": False, "error": "这个项目已经不在库里了"}), 404

    if on:
        with db.tx() as c:
            c.execute(
                "INSERT INTO project_server(project_path, server_id) VALUES(?,?) "
                "ON CONFLICT(project_path, server_id) DO NOTHING",
                (path, sid),
            )
    else:
        db.execute("DELETE FROM project_server WHERE project_path=? AND server_id=?", (path, sid))
    db.audit("修改部署关联", f"{proj['dirname']} {'→' if on else '×'} server#{sid}")
    return jsonify({"ok": True, "linked": on})


@bp.post("/api/server/reconnect")
def api_server_reconnect():
    """只重连这一台，结果直接回给页面 —— 不用整页刷新，也不用等全部机器跑一遍.

    连不上时的自动降级（兼容老算法）在 monitor.collect_one 里做，
    这里只负责把「成没成、为什么、有没有自动修好」如实带回去。
    """
    sid = (request.form.get("id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "缺少服务器 id"}), 400

    row = db.query_one("SELECT * FROM server WHERE id=?", (sid,))
    if row is None:
        return jsonify({"ok": False, "error": "这台服务器不在列表里了，回去刷新一下"}), 404

    result = monitor.collect_one(monitor._server_dict(row))
    monitor.store([result])
    db.audit("重连服务器", f"{row['name']} · {'成功' if result['online'] else '失败'}")

    return jsonify(
        {
            "ok": True,
            "online": bool(result["online"]),
            "error": result.get("error", ""),
            "auto_fixed": result.get("auto_fixed", ""),
        }
    )
