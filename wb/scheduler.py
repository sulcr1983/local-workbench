"""定时任务：巡检 + 项目档案重扫 + 到期检查 + 阈值告警.

锁 3.x（APScheduler 4 改了 API，升级要重写这个文件）。
三个 job：
  wb-monitor       每 N 分钟：连服务器采一轮，落库，超阈值就推
  wb-project-scan  每 N 分钟：重扫项目档案，README / git 提交不用手点刷新
  wb-expiry        每 N 小时：刷域名/证书/云资源，≤7 天到期就推
所有 job 只写 DB，页面重新读 —— 不做服务端推状态（TECH_SPEC §4 规则 4）。
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import db, expiry, monitor, notify

log = logging.getLogger("wb.scheduler")

_scheduler: BackgroundScheduler | None = None

DEFAULT_MONITOR_MINUTES = 10
DEFAULT_EXPIRY_HOURS = 6
# 项目档案重扫：全量扫盘比巡检重，默认一小时一次；改了 README / 提交代码不用手点刷新
DEFAULT_PROJECT_SCAN_MINUTES = 60


def _settings() -> dict:
    import json

    raw = db.scalar("SELECT cfg FROM ui_cfg WHERE id=1", default="")
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
    return {
        "monitor_minutes": int(data.get("monitor_minutes") or DEFAULT_MONITOR_MINUTES),
        "expiry_hours": int(data.get("expiry_hours") or DEFAULT_EXPIRY_HOURS),
        "project_scan_minutes": int(data.get("project_scan_minutes") or DEFAULT_PROJECT_SCAN_MINUTES),
        "alerts": bool(data.get("alerts", True)),
    }


# ------------------------------------------------------------------ 任务体
def job_monitor() -> dict:
    """采一轮 + 清理 + 阈值告警."""
    stats = monitor.refresh()
    pruned = monitor.prune()
    pushed = 0

    if _settings()["alerts"]:
        for row in db.query(
            """SELECT s.id, s.name, s.alert_mem, s.alert_disk, s.alert_load,
                      m.mem_pct, m.disk_pct, m.load1, m.error
               FROM server s
               JOIN metric m ON m.id = (SELECT MAX(id) FROM metric WHERE server_id = s.id)
               WHERE s.enabled = 1"""
        ):
            reasons = []
            if row["mem_pct"] is not None and row["mem_pct"] >= row["alert_mem"]:
                reasons.append(f"内存 {row['mem_pct']}%（阈值 {row['alert_mem']}%）")
            if row["disk_pct"] is not None and row["disk_pct"] >= row["alert_disk"]:
                reasons.append(f"磁盘 {row['disk_pct']}%（阈值 {row['alert_disk']}%）")
            if row["load1"] is not None and row["load1"] >= row["alert_load"]:
                reasons.append(f"负载 {row['load1']}（阈值 {row['alert_load']}）")
            if not reasons:
                continue
            result = notify.push(
                f"【告警】{row['name']} 指标超阈值",
                "、".join(reasons),
                dedup_key=f"alert:server:{row['id']}",
            )
            if result.get("ok") and not result.get("skipped"):
                pushed += 1

    log.info("巡检完成：%s；清理 %s 条；推送 %s 条", stats, pruned.get("removed"), pushed)
    return {"refresh": stats, "pruned": pruned, "pushed": pushed}


def job_expiry() -> dict:
    """刷到期 + 临期提醒."""
    stats = expiry.refresh_all()
    pushed = 0
    for row in db.query(
        """SELECT id, name, kind, expire FROM expiry
           WHERE expire IS NOT NULL AND expire <= date('now','localtime','+7 day')"""
    ):
        days = expiry.days_left(row["expire"])
        label, _cls = expiry.level(days)
        result = notify.push(
            f"【到期】{row['name']} · {label}",
            f"{expiry.KIND_LABEL.get(row['kind'], row['kind'])} 到期日 {row['expire']}",
            dedup_key=f"expiry:{row['id']}:{row['expire']}",
        )
        if result.get("ok") and not result.get("skipped"):
            pushed += 1

    log.info("到期检查完成：%s；推送 %s 条", stats, pushed)
    return {"refresh": stats, "pushed": pushed}


def job_project_scan() -> dict:
    """定时重扫项目档案：改完 README / 提交代码，不用手点「刷新全部」.

    跟顶栏「刷新全部」走同一段 scan.sync，结果一致；只写 DB，不推页面状态。
    异常交给 _wrap 记 sched_run + 日志，不静默吞掉。
    """
    from . import scan

    roots = [r["path"] for r in db.query("SELECT path FROM root ORDER BY added_at")]
    if not roots:
        log.info("项目定时重扫跳过：还没设置根目录")
        return {"scanned": 0}
    result = scan.sync(roots)
    db.audit("扫描项目", f"{result['scanned']} 个 · 定时重扫")
    log.info("项目定时重扫完成：%s 个", result["scanned"])
    return result


# ------------------------------------------------------------------ 调度器
def _wrap(job_id: str, fn: Callable[[], object]) -> Callable[[], None]:
    """把 job 包一层：跑完记 sched_run，异常只记日志不炸调度器."""

    def runner() -> None:
        try:
            fn()
        except Exception as err:  # noqa: BLE001 - 单次失败不该停掉整个调度
            log.warning("%s 执行失败：%s", job_id, err)
            _record(job_id, False, str(err))
        else:
            _record(job_id, True)

    runner.__name__ = f"runner_{job_id}"
    return runner


def _record(job_id: str, ok: bool, error: str = "") -> None:
    """记录一次运行结果；写库失败只记日志，绝不让 job 崩掉."""
    try:
        with db.tx() as c:
            c.execute(
                "INSERT INTO sched_run(job_id, ts, ok, error) VALUES(?,?,?,?)",
                (job_id, db.now(), 1 if ok else 0, str(error)[:200]),
            )
    except Exception as err:  # noqa: BLE001 - 记录失败不该阻断调度
        log.warning("写 sched_run 失败（不影响调度）：%s", err)


def start(monitor_minutes: int | None = None, expiry_hours: int | None = None,
          project_scan_minutes: int | None = None) -> BackgroundScheduler:
    """启动后台调度；重复调用只生效一次."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    conf = _settings()
    mm = monitor_minutes or conf["monitor_minutes"]
    eh = expiry_hours or conf["expiry_hours"]
    psm = project_scan_minutes or conf["project_scan_minutes"]

    sched = BackgroundScheduler(timezone="Asia/Shanghai", daemon=True)
    sched.add_job(
        _wrap("wb-monitor", job_monitor),
        trigger=IntervalTrigger(minutes=mm),
        id="wb-monitor",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        _wrap("wb-expiry", job_expiry),
        trigger=IntervalTrigger(hours=eh),
        id="wb-expiry",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        _wrap("wb-project-scan", job_project_scan),
        trigger=IntervalTrigger(minutes=psm),
        id="wb-project-scan",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    log.info("调度已启动：巡检每 %s 分钟、项目重扫每 %s 分钟、到期每 %s 小时", mm, psm, eh)
    return sched


def stop() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def running() -> bool:
    return _scheduler is not None and _scheduler.running


def jobs_state() -> list[dict]:
    """给设置页显示「下次什么时候跑」+ 最近一次运行结果（失败要让页面看见）."""
    if _scheduler is None:
        return []
    out = []
    for job in _scheduler.get_jobs():
        nxt = getattr(job, "next_run_time", None)
        last = db.query_one(
            "SELECT ts, ok, error FROM sched_run WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (job.id,),
        )
        out.append(
            {
                "id": job.id,
                "next": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else "未排期",
                "trigger": str(job.trigger),
                "last_ts": last["ts"] if last else "",
                "last_ok": bool(last["ok"]) if last else None,
                "last_error": (last["error"] if last else "")[:120],
            }
        )
    return out


def autostart() -> bool:
    """app.py 用：除非 WB_NO_SCHED=1，否则启动调度."""
    if os.environ.get("WB_NO_SCHED") == "1":
        return False
    start()
    return True
