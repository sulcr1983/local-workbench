"""巡检编排：并发采集（全在事务外）+ 节流落库（毫秒级短事务）.

TECH_SPEC §5.3 ②：SSH 超时 18s 绝不能在事务里等，否则写锁被占 18 秒。
所以流程固定两段：collect_all() 拿完数据 → store() 只做 INSERT。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import db, probe, ssh

COLLECT_TIMEOUT = 18
THROTTLE_SECONDS = 300   # 最少 5 分钟一写（TECH_SPEC §5.3 ⑤）
KEEP_DAYS = 7
KEEP_ROWS = 5000


def _server_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "host": row["host"],
        "port": row["port"],
        "user": row["user"],
        "auth": row["auth"],
        "secret": row["secret"],
        "passphrase": row["passphrase"],
        "jump": row["jump"],
        "legacy_kex": bool(row["legacy_kex"]) if "legacy_kex" in row.keys() else False,
    }


def _collect_once(server: dict) -> dict:
    """连一次、跑一次巡检。失败不抛，折成 online=0 + 中文原因."""
    server_id = server.get("id")
    client = None
    try:
        client = ssh.open_session(server)
        out, err, code = ssh.run(client, probe.PROBE_CMD, timeout=COLLECT_TIMEOUT)
        if code != 0 and not out.strip():
            return {
                "server_id": server_id,
                "online": 0,
                "snap": None,
                "error": (err or f"巡检命令返回码 {code}")[:300],
            }
        snap = probe.parse(out)
        return {"server_id": server_id, "online": 1, "snap": snap, "error": ""}
    except ssh.SSHError as err:
        return {"server_id": server_id, "online": 0, "snap": None, "error": str(err)[:300]}
    except Exception as err:  # noqa: BLE001 - 采集层不允许把异常抛到页面
        return {"server_id": server_id, "online": 0, "snap": None, "error": f"{type(err).__name__}: {err}"[:300]}
    finally:
        ssh.close(client)


def _collect_via_cli(server: dict) -> dict:
    """paramiko 连不上时，用系统 ssh 客户端再试一次.

    OpenSSH 9.x 默认优先 sntrup761x25519（后量子 kex），paramiko 至今不支持。
    这不是服务器的问题，是 Python 库的短板 —— 本机有 ssh 客户端就用它兜底。
    """
    server_id = server.get("id")
    try:
        out, _err, code = ssh.run_via_cli(server, probe.PROBE_CMD)
    except ssh.SSHError as err:
        return {"server_id": server_id, "online": 0, "snap": None, "error": str(err)[:300]}
    except Exception as err:  # noqa: BLE001
        return {"server_id": server_id, "online": 0, "snap": None,
                "error": f"系统 ssh 出错：{type(err).__name__}: {err}"[:300]}
    if code != 0 and not out.strip():
        return {"server_id": server_id, "online": 0, "snap": None,
                "error": f"系统 ssh 返回码 {code}，没有输出"[:300]}
    return {"server_id": server_id, "online": 1, "snap": probe.parse(out), "error": ""}


def _looks_like_kex_issue(text: str) -> bool:
    low = (text or "").lower()
    return "握手谈不拢" in (text or "") or "kex" in low or "加密算法" in (text or "")


def _remember_legacy(server_id) -> None:
    """这台机器必须用兼容模式才能连 —— 记下来，下次别再试错一遍."""
    if not server_id:
        return
    try:
        db.execute("UPDATE server SET legacy_kex=1, updated_at=? WHERE id=?", (db.now(), server_id))
        db.audit("自动开启兼容模式", f"server#{server_id} 只能用老 kex 握手")
    except Exception:  # noqa: BLE001 - 记不住也不能影响采集结果
        pass


def collect_one(server: dict) -> dict:
    """采集一台；连不上会自动用兼容模式再试一次.

    老服务器（或中间设备改过握手）只认 group1-sha1 这类算法，默认策略直接拒。
    这事不该让用户去翻文档开开关 —— 系统自己降级试一次，通了就把开关记住。
    """
    result = _collect_once(server)
    if result["online"]:
        return result

    if _looks_like_kex_issue(result["error"]):
        # 第一层：老服务器只认 group1-sha1，开 legacy 再试 paramiko
        if not server.get("legacy_kex"):
            retry = _collect_once({**server, "legacy_kex": True})
            if retry["online"]:
                _remember_legacy(server.get("id"))
                retry["auto_fixed"] = "自动切到兼容模式后连上了"
                return retry

        # 第二层：新服务器只认后量子 kex（sntrup…），paramiko 不支持 → 系统 ssh
        if ssh.cli_available():
            via_cli = _collect_via_cli(server)
            if via_cli["online"]:
                via_cli["auto_fixed"] = "这台只认新算法，已自动改用系统 ssh 连上"
                return via_cli
            result["error"] = (
                f"{result['error']}\n（兼容老算法和系统 ssh 都试过了，都不通）"
            )[:400]
        else:
            result["error"] = (
                f"{result['error']}\n（这台只认新加密算法，paramiko 不支持；"
                f"本机也没找到 ssh 客户端，装一个 OpenSSH 客户端就能连）"
            )[:400]

    return result


def collect_all(servers: list[dict], workers: int = 4) -> list[dict]:
    """并发 4；单台失败不影响其他（TECH_SPEC §7.2）."""
    if not servers:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(servers))) as pool:
        return list(pool.map(collect_one, servers))


def _last_metric(server_id: int):
    return db.query_one(
        "SELECT * FROM metric WHERE server_id=? ORDER BY id DESC LIMIT 1", (server_id,)
    )


def _changed(last, online: int, snap) -> bool:
    if last is None:
        return True
    if int(last["online"]) != online:
        return True
    if not online or snap is None:
        return False
    for col, val in (
        ("mem_pct", snap.mem_pct),
        ("disk_pct", snap.disk_pct),
        ("load1", snap.load1),
    ):
        if last[col] is None and val is None:
            continue
        if last[col] is None or val is None:
            return True
        if abs(float(last[col]) - float(val)) >= 0.05:
            return True
    return False


def _too_soon(last) -> bool:
    if last is None:
        return False
    try:
        prev = datetime.strptime(last["ts"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return (datetime.now() - prev).total_seconds() < THROTTLE_SECONDS


def store(results: list[dict]) -> dict:
    """落库：值没变且不到 5 分钟就跳过；写入全是短事务."""
    written = 0
    skipped = 0
    ts = db.now()
    for r in results:
        sid = r["server_id"]
        snap = r.get("snap")
        last = _last_metric(sid)

        if not _changed(last, r["online"], snap) and _too_soon(last):
            skipped += 1
            continue

        with db.tx() as c:
            c.execute(
                """INSERT INTO metric(server_id, ts, online, hostname, load1,
                                      os_name, kernel, uptime_text,
                                      mem_used, mem_total, mem_pct,
                                      disk_used, disk_total, disk_pct,
                                      containers, ports, port_json, error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sid,
                    ts,
                    r["online"],
                    (snap.hostname if snap else None),
                    (snap.load1 if snap else None),
                    (snap.os_name if snap else None),
                    (snap.kernel if snap else None),
                    (snap.uptime_text if snap else None),
                    (snap.mem_used if snap else None),
                    (snap.mem_total if snap else None),
                    (snap.mem_pct if snap else None),
                    (snap.disk_used if snap else None),
                    (snap.disk_total if snap else None),
                    (snap.disk_pct if snap else None),
                    (",".join(snap.containers) if snap else None),
                    (",".join(snap.ports) if snap else None),
                    # 端口明细：谁在听、绑哪个地址。存 JSON 免得再开一张表
                    (json.dumps(snap.port_details, ensure_ascii=False)
                     if snap and snap.port_details else None),
                    r.get("error") or None,
                ),
            )
        written += 1

    return {"written": written, "skipped": skipped, "at": ts}


def prune() -> dict:
    """metric 只留 7 天 / 每台 5000 条（TECH_SPEC §10）."""
    before = db.scalar("SELECT COUNT(*) FROM metric", default=0)
    with db.tx() as c:
        c.execute("DELETE FROM metric WHERE ts < datetime('now', 'localtime', ?)", (f"-{KEEP_DAYS} days",))
        c.execute(
            """DELETE FROM metric WHERE id NOT IN (
                   SELECT id FROM (
                       SELECT id, ROW_NUMBER() OVER (PARTITION BY server_id ORDER BY id DESC) AS rn
                       FROM metric
                   ) WHERE rn <= ?
               )""",
            (KEEP_ROWS,),
        )
    after = db.scalar("SELECT COUNT(*) FROM metric", default=0)
    return {"before": before, "after": after, "removed": before - after}


def refresh(workers: int = 4) -> dict:
    """页面「刷新全部」与定时任务共用入口."""
    rows = db.query("SELECT * FROM server WHERE enabled=1 ORDER BY id")
    servers = [_server_dict(r) for r in rows]
    if not servers:
        return {"total": 0, "online": 0, "written": 0, "skipped": 0}

    results = collect_all(servers, workers=workers)
    stats = store(results)
    stats["total"] = len(results)
    stats["online"] = sum(1 for r in results if r["online"])
    return stats
