"""写接口（POST，form 直提，恒定返回 {"ok": bool, "error": str?}）.

约定（TECH_SPEC §6.2）：
- 不解析 JSON body，前端一律 FormData。
- 成功即写审计。
- 破坏性操作前端二次确认；后端只做参数校验与落库。
"""

from __future__ import annotations

import os
import subprocess
import sys

from flask import Blueprint, jsonify, request

from .. import db
from .projects import lookup

bp = Blueprint("api", __name__, url_prefix="/api")

# 字段白名单：绝不把请求里的字段名直接拼进 SQL
FIELD_MAP = {"name": "user_name", "status": "user_status", "note": "user_note"}
STATUS_VALUES = {"活跃", "放缓", "停滞", "可能废弃", "进行中", "已完成", "归档", ""}
BULK_ACTIONS = {"pin", "unpin", "archive", "unarchive", "done", "ignore"}


def ok(**extra):
    return jsonify({"ok": True, **extra})


def fail(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


# ---------------------------------------------------------------- 通用
@bp.post("/open")
def open_path():
    """在资源管理器里打开项目目录；只允许打开已知根目录下的路径."""
    rel = (request.form.get("rel") or "").strip()
    row, _root = lookup(rel)
    if row is None:
        return fail("项目不存在，可能已经被移走了")
    if not os.path.isdir(row["path"]):
        return fail("这个目录已经不在了")

    try:
        if sys.platform == "win32":
            os.startfile(row["path"])  # noqa: S606 - 本地工具，路径已过白名单
        elif sys.platform == "darwin":
            subprocess.Popen(["open", row["path"]])
        else:
            subprocess.Popen(["xdg-open", row["path"]])
    except OSError as err:
        return fail(f"打不开：{err}", 500)

    db.audit("打开目录", row["path"])
    return ok()


# ---------------------------------------------------------------- 引擎
@bp.post("/engine/state")
def engine_state():
    """收编 / 剔除 / 退回候选。

    只改 project.state 一个字段 —— 不删数据、不动磁盘，
    所以「剔掉」是可逆的，随时能在引擎页恢复。
    """
    to = (request.form.get("to") or "").strip()
    if to not in ("auto", "candidate", "dropped"):
        return fail("不认识的目标状态")

    targets = request.form.getlist("paths")
    one = (request.form.get("path") or "").strip()
    if one:
        targets.append(one)
    targets = [t.strip() for t in targets if t and t.strip()]
    if not targets:
        return fail("没说要改哪条")

    # 收编/剔掉 = 人拍过板，打个锁让重扫别覆盖；退回候选 = 交还给引擎，解锁
    locked = 0 if to == "candidate" else 1
    marks = ",".join("?" * len(targets))
    with db.tx() as c:
        changed = c.execute(
            f"UPDATE project SET state=?, state_locked=? WHERE path IN ({marks})",
            (to, locked, *targets),
        ).rowcount

    label = {"auto": "收编", "candidate": "退回候选", "dropped": "剔除"}[to]
    db.audit(f"引擎{label}", f"{changed} 条")
    return ok(changed=changed)


# ---------------------------------------------------------------- 项目
@bp.post("/project/update")
def project_update():
    rel = (request.form.get("rel") or "").strip()
    field = (request.form.get("field") or "").strip()
    value = (request.form.get("value") or "").strip()

    row, _root = lookup(rel)
    if row is None:
        return fail("项目不存在，可能已经被移走了")

    col = FIELD_MAP.get(field)
    if col is None:
        return fail(f"不支持的字段：{field}")

    # 名称不能清空：空值会把 user_name 置 NULL、悄悄回退成自动命名，
    # 前端却提示「已保存」—— 用户以为改成功了，实际没改，必须明确拒绝。
    if field == "name" and not value:
        return fail("项目名称不能为空")
    if field == "name" and len(value) > 120:
        return fail("名称最长 120 个字")
    if field == "status" and value not in STATUS_VALUES:
        return fail(f"不支持的状态：{value}")
    if field == "note" and len(value) > 500:
        return fail("备注最长 500 个字")

    db.execute(f"UPDATE project SET {col}=? WHERE path=?", (value or None, row["path"]))
    db.audit("修改项目", f"{row['dirname']} · {field}={value[:40]}")
    return ok(value=value)


@bp.post("/project/server")
def project_server_link():
    """项目 ↔ 服务器 关联/解除（原型里的「部署到哪台」）."""
    rel = (request.form.get("rel") or "").strip()
    sid = request.form.get("sid")
    on = (request.form.get("on") or "1") == "1"
    row, _root = lookup(rel)
    if row is None:
        return fail("项目不存在")
    if not sid:
        return fail("缺少服务器 id")

    if on:
        with db.tx() as c:
            c.execute(
                "INSERT INTO project_server(project_path, server_id) VALUES(?,?) "
                "ON CONFLICT(project_path, server_id) DO NOTHING",
                (row["path"], sid),
            )
    else:
        db.execute("DELETE FROM project_server WHERE project_path=? AND server_id=?", (row["path"], sid))
    db.audit("修改部署关联", f"{row['dirname']} → {'关联' if on else '解除'} server#{sid}")
    return ok(linked=on)


@bp.post("/project/local")
def project_run_local():
    """这个项目就跑在这台电脑上 —— 不用挂 SSH 服务器，但「部署在哪」得有答案."""
    rel = (request.form.get("rel") or "").strip()
    on = (request.form.get("on") or "1") == "1"
    row, _root = lookup(rel)
    if row is None:
        return fail("项目不存在")

    db.execute("UPDATE project SET run_local=? WHERE path=?", (1 if on else 0, row["path"]))
    db.audit("修改运行位置", f"{row['dirname']} · {'本机' if on else '取消本机'}")
    return ok(local=on)


@bp.post("/project/togglepin")
def project_togglepin():
    rel = (request.form.get("rel") or "").strip()
    row, _root = lookup(rel)
    if row is None:
        return fail("项目不存在")

    new = 0 if row["user_pinned"] else 1
    db.execute("UPDATE project SET user_pinned=? WHERE path=?", (new, row["path"]))
    db.audit("置顶项目" if new else "取消置顶", row["dirname"])
    return ok(pinned=bool(new))


@bp.post("/projects/bulk")
def projects_bulk():
    action = (request.form.get("action") or "").strip()
    paths = [p for p in request.form.getlist("paths") if p]
    if action not in BULK_ACTIONS:
        return fail(f"不支持的批量动作：{action}")
    if not paths:
        return fail("没有选中任何项目")

    marks = ", ".join("?" for _ in paths)
    if action == "pin":
        sql = f"UPDATE project SET user_pinned=1 WHERE path IN ({marks})"
        args = paths
    elif action == "unpin":
        sql = f"UPDATE project SET user_pinned=0 WHERE path IN ({marks})"
        args = paths
    elif action == "archive":
        sql = f"UPDATE project SET user_status='归档' WHERE path IN ({marks})"
        args = paths
    elif action == "done":
        sql = f"UPDATE project SET user_status='已完成' WHERE path IN ({marks})"
        args = paths
    elif action == "ignore":
        # 「从清单移除」：不删数据，只是不再出现在默认列表里，随时能恢复
        sql = f"UPDATE project SET user_status='忽略' WHERE path IN ({marks})"
        args = paths
    else:  # unarchive
        sql = f"UPDATE project SET user_status=NULL WHERE path IN ({marks})"
        args = paths

    with db.tx() as c:
        c.execute(sql, tuple(args))
    db.audit("批量操作", f"{action} · {len(paths)} 项")
    return ok(count=len(paths))


@bp.post("/project/rescan")
def project_rescan():
    """只重扫一个项目 —— 改完 README / 提交完代码，不该等整盘扫完才能看到.

    这是用户提的：「每个项目都要单独设置刷新，不然更改了也没变动」。
    跟全量扫描走同一段 UPSERT，所以两者结果一致，不会出现"单独刷和刷全部不一样"。
    """
    rel = (request.form.get("rel") or "").strip()
    row, _root = lookup(rel)
    if row is None:
        return fail("项目不存在，可能已经被移走了")

    from .. import scan
    from .settings import load_ui

    try:
        rules = load_ui().get("rules")
    except Exception:  # noqa: BLE001 - 规则读不到也要能刷
        rules = None
    try:
        result = scan.sync_one(row["path"], rules)
    except FileNotFoundError:
        return fail("这个目录已经不在了（被删或改名）", 404)
    except Exception as err:  # noqa: BLE001
        return fail(f"重扫失败：{str(err).splitlines()[0][:120]}", 500)

    db.audit("重扫项目", f"{row['dirname']}")
    return ok(**result)


@bp.post("/refresh-all")
def refresh_all():
    """顶栏「刷新全部」：重扫所有根目录下的项目.

    之前这个按钮是 `disabled` 且没有任何点击处理 —— 属于没接通的摆设。
    """
    from .. import scan

    roots = [r["path"] for r in db.query("SELECT path FROM root ORDER BY added_at")]
    if not roots:
        return fail("还没设置项目文件夹，先去「设置」加一个")

    try:
        result = scan.sync(roots)
    except Exception as err:  # noqa: BLE001
        return fail(f"扫描失败：{str(err).splitlines()[0][:120]}", 500)

    db.audit("扫描项目", f"{result['scanned']} 个 · 刷新全部")
    return ok(**result)
