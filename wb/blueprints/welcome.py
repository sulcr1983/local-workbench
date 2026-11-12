"""新手向导：选项目文件夹（可多选）→ 认一下本机 SSH 里的服务器 → 开始.

三条原则：
1. 每一步都能跳过，不做成"必须填完才能用"。
2. 扫描出来的东西一律是**候选**，默认不勾选，要人点过才入库。
3. 不走网络：只读本机文件，判断"能不能连"由用户点「测试连接」触发。
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from .. import db, discover, scan

bp = Blueprint("welcome", __name__)


def is_done() -> bool:
    return db.get_meta("onboarding_done") == "1"


def _set_done() -> None:
    with db.tx() as c:
        db.set_meta("onboarding_done", "1", c)


@bp.get("/welcome")
def index():
    return render_template(
        "welcome.html",
        page_title="先配两件事",
        page_code="SETUP / 000",
        crumb="开始使用",
        active="",
        done=is_done(),
        roots=[r["path"] for r in db.query("SELECT path FROM root ORDER BY added_at")],
        counts={},
    )


# ---------------------------------------------------------------- 目录浏览
@bp.get("/api/fs/drives")
def api_drives():
    return jsonify({"ok": True, "drives": discover.drives()})


@bp.get("/api/fs/list")
def api_list():
    path = (request.args.get("path") or "").strip() or (discover.drives()[0] if discover.drives() else "/")
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": f"目录不存在：{path}"}), 400
    return jsonify(
        {
            "ok": True,
            "path": path,
            "parent": discover.parent_of(path),
            "dirs": discover.list_dirs(path),
        }
    )


# ---------------------------------------------------------------- 第一步
@bp.post("/api/welcome/roots")
def api_roots():
    """多选：一次提交多个目录，去重后入库并立刻扫一遍."""
    paths = [p.strip().strip('"') for p in request.form.getlist("paths") if p.strip()]
    paths += [p.strip().strip('"') for p in (request.form.get("paths_text") or "").splitlines() if p.strip()]

    added: list[str] = []
    skipped: list[str] = []
    for raw in paths:
        full = os.path.abspath(raw)
        if not os.path.isdir(full):
            skipped.append(raw)
            continue
        with db.tx() as c:
            c.execute(
                "INSERT INTO root(path, added_at) VALUES(?,?) ON CONFLICT(path) DO NOTHING",
                (full, db.now()),
            )
        added.append(full)

    if not added:
        return jsonify({"ok": False, "error": "没有可用的目录", "skipped": skipped}), 400

    result = scan.sync(added)
    db.audit("向导-添加根目录", f"{len(added)} 个 · 扫到 {result['scanned']} 个项目")
    return jsonify({"ok": True, "added": added, "skipped": skipped, "scanned": result["scanned"]})


# ---------------------------------------------------------------- 第二步
@bp.post("/api/welcome/discover")
def api_discover():
    """扫本机 SSH 与项目文档，全部作为候选返回（不入库）."""
    roots = [r["path"] for r in db.query("SELECT path FROM root ORDER BY added_at")]
    scope = (request.form.get("scope") or db.scalar("SELECT cfg FROM ui_cfg WHERE id=1", default="") or "")
    from .settings import load_ui

    scope = request.form.get("scope") or load_ui().get("scan_scope", "full")
    ssh = discover.ssh_candidates()
    hosts = discover.project_hosts(roots, scope=scope) if roots else []
    return jsonify({"ok": True, "scope": scope, "ssh": ssh, "project_hosts": hosts})


@bp.post("/api/welcome/servers")
def api_servers():
    """把勾选的候选建成服务器记录。auth=key_path（有密钥）或 agent，secret 只存路径."""
    import json

    raw = request.form.get("items") or "[]"
    try:
        items = json.loads(raw)
    except ValueError:
        return jsonify({"ok": False, "error": "数据格式不对"}), 400
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "没有勾选任何服务器"}), 400

    created = skipped = 0
    ts = db.now()
    for item in items[:50]:
        name = str(item.get("name") or item.get("host") or "").strip()
        host = str(item.get("host") or "").strip()
        if not name or not host:
            skipped += 1
            continue
        key = str(item.get("key") or "").strip()
        try:
            port = int(item.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        try:
            with db.tx() as c:
                c.execute(
                    """INSERT INTO server(name, host, port, user, auth, secret, passphrase,
                       jump, grp, note, alert_mem, alert_disk, alert_load, legacy_kex,
                       created_at, updated_at)
                       VALUES(?,?,?,?,?,?,'','','',? ,85,90,4.0,0,?,?)""",
                    (
                        name, host, port, str(item.get("user") or "root"),
                        "key_path" if key else "agent",
                        key,
                        "向导导入",
                        ts, ts,
                    ),
                )
            created += 1
        except Exception:  # noqa: BLE001 - 重名等唯一约束，跳过即可
            skipped += 1

    db.audit("向导-导入服务器", f"{created} 台（来自本机 SSH 配置）")
    return jsonify({"ok": True, "created": created, "skipped": skipped})


@bp.post("/api/welcome/scope")
def api_scan_scope():
    """扫描范围：full = 什么文件都能扫；safe = 只看文档，不碰 .env / config."""
    from .settings import _save_ui

    scope = (request.form.get("scope") or "full").strip()
    if scope not in ("full", "safe"):
        return jsonify({"ok": False, "error": "不认识的取值范围"}), 400
    _save_ui({"scan_scope": scope})
    db.audit("设置扫描范围", "无限制" if scope == "full" else "保守")
    return jsonify({"ok": True, "scope": scope})


# ---------------------------------------------------------------- 结束
@bp.post("/api/welcome/done")
def api_done():
    _set_done()
    db.audit("向导-完成", "")
    return jsonify({"ok": True, "next": url_for("projects.index")})


@bp.post("/api/welcome/skip")
def api_skip():
    _set_done()
    db.audit("向导-跳过", "")
    return redirect(url_for("projects.index"))
