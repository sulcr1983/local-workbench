"""SQLite 连接、迁移与 DAO.

铁律（TECH_SPEC §5.3）：
1. 每线程一个连接，不共享 —— 用 thread-local，不用 check_same_thread=False。
2. 每个连接都设 busy_timeout / journal_mode / synchronous / foreign_keys。
3. 写事务 BEGIN IMMEDIATE，避免读升级写时才发现冲突。
4. 事务里绝不做网络 I/O（SSH / HTTP 全在事务外）。
5. data/ 必须是本地盘，UNC 路径直接拒绝启动。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable

from . import config

SCHEMA_VERSION = 8


class DBError(RuntimeError):
    """数据库层面的、必须让用户看见的错误（不许静默吞掉）."""


_local = threading.local()
_db_path: str = config.DB_PATH


def configure(path: str) -> None:
    """切换库路径（测试用）；同时丢弃当前线程的旧连接."""
    global _db_path
    _db_path = os.path.abspath(path)
    close_thread_conn()


def db_path() -> str:
    return _db_path


def close_thread_conn() -> None:
    c = getattr(_local, "c", None)
    if c is not None:
        try:
            c.close()
        finally:
            _local.c = None


def conn() -> sqlite3.Connection:
    """当前线程的连接（懒建）."""
    c = getattr(_local, "c", None)
    if c is not None:
        return c

    if config.is_network_path(_db_path):
        raise DBError(
            f"data 目录位于网络路径，WAL 模式在网络盘上会损坏数据库。\n"
            f"请把 data 目录移到本地磁盘后重新启动：{os.path.dirname(_db_path)}"
        )

    os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    c = sqlite3.connect(_db_path, timeout=5, isolation_level=None)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA foreign_keys=ON")
    except sqlite3.DatabaseError as err:
        # 文件不是合法库（被覆盖、半截下载、磁盘故障）都会走到这里
        c.close()
        raise DBError(
            f"数据库无法打开，可能已经损坏（不会覆盖你的数据）：{_db_path}\n"
            f"原始错误：{err}\n"
            f"{_recover_hint()}"
        ) from err
    _local.c = c
    return c


@contextmanager
def tx():
    """写事务：BEGIN IMMEDIATE → 干活 → COMMIT，异常回滚."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        c.execute("COMMIT")


# ------------------------------------------------------------------ 查询
def query(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, tuple(args)).fetchall()


def query_one(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn().execute(sql, tuple(args)).fetchone()


def execute(sql: str, args: Iterable[Any] = ()) -> int:
    """单条写（自带隐式事务，不用于多条原子写）."""
    cur = conn().execute(sql, tuple(args))
    return cur.lastrowid or 0


def scalar(sql: str, args: Iterable[Any] = (), default: Any = None) -> Any:
    row = query_one(sql, args)
    if row is None:
        return default
    val = row[0]
    return default if val is None else val


# ------------------------------------------------------------------ meta
def get_meta(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: str, cur: sqlite3.Connection | None = None) -> None:
    sql = (
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    (cur or conn()).execute(sql, (key, str(value)))


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 备份
# 备份目录放在库文件的同级（data/backups/），不往 data/ 里塞散文件。
BACKUP_DIRNAME = "backups"
BACKUP_KEEP = 7  # 保留份数


def backup_dir() -> str:
    return os.path.join(os.path.dirname(_db_path), BACKUP_DIRNAME)


def backup_now(tag: str = "") -> str:
    """把当前库做一份安全点快照；成功返回路径，失败抛 DBError（绝不静默）.

    用 sqlite3.backup 而不是「直接拷文件」：WAL 模式下直接拷可能拿到不含
    最新写入的旧镜像。备份同时会刷出 .db-wal 的持久化快照。
    """
    src = conn()  # 确保连接可读
    os.makedirs(backup_dir(), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    path = os.path.join(backup_dir(), f"workbench.{stamp}{suffix}.db")
    dst = sqlite3.connect(path)
    try:
        with dst:
            src.backup(dst)
    except sqlite3.Error as err:
        dst.close()
        raise DBError(f"备份失败（{path}）：{err}") from err
    finally:
        dst.close()
    _prune_backups()
    return path


def _prune_backups() -> int:
    """只留最近 BACKUP_KEEP 份；删不动的就放过（不因为备份清理失败阻断主流程）."""
    try:
        names = sorted(
            f for f in os.listdir(backup_dir())
            if f.startswith("workbench.") and f.endswith(".db")
        )
        for old in names[:-BACKUP_KEEP]:
            os.remove(os.path.join(backup_dir(), old))
        return len(names) - BACKUP_KEEP
    except OSError as err:
        logging.getLogger("wb.db").warning("备份轮转失败：%s", err)
        return 0


def latest_backup() -> str | None:
    """最近一次备份的路径（没有就 None）."""
    try:
        names = [
            f for f in os.listdir(backup_dir())
            if f.startswith("workbench.") and f.endswith(".db")
        ]
    except OSError:
        return None
    if not names:
        return None
    return os.path.join(backup_dir(), sorted(names)[-1])


def _recover_hint() -> str:
    """错误提示里的恢复指引 —— 必须指向**真实存在**的东西，不编路径."""
    latest = latest_backup()
    if latest:
        return f"请从备份恢复：{latest}（或把同目录 backups/ 里的任意一份拷回原位）"
    return "没有找到自动备份；请回忆最近一次手动拷贝，或检查 data/backups/ 目录"


# ------------------------------------------------------------------ schema
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS root (path TEXT PRIMARY KEY, added_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS server (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  host        TEXT NOT NULL,
  port        INTEGER NOT NULL DEFAULT 22,
  user        TEXT NOT NULL DEFAULT 'root',
  auth        TEXT NOT NULL DEFAULT 'key_path',
  secret      TEXT DEFAULT '',
  passphrase  TEXT DEFAULT '',
  jump        TEXT DEFAULT '',
  grp         TEXT DEFAULT '',
  note        TEXT DEFAULT '',
  alert_mem   INTEGER NOT NULL DEFAULT 85,
  alert_disk  INTEGER NOT NULL DEFAULT 90,
  alert_load  REAL    NOT NULL DEFAULT 4.0,
  enabled     INTEGER NOT NULL DEFAULT 1,
  legacy_kex  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project (
  path        TEXT PRIMARY KEY,
  dirname     TEXT NOT NULL,
  name_auto   TEXT,
  name_src    TEXT,
  name_conf   REAL,
  stack       TEXT,
  active      TEXT,
  is_git      INTEGER NOT NULL DEFAULT 0,
  last_commit TEXT,
  dirty       INTEGER NOT NULL DEFAULT 0,
  dirty_n     INTEGER NOT NULL DEFAULT 0,   -- 未提交文件数
  unpushed_n  INTEGER NOT NULL DEFAULT 0,   -- 未推送提交数（没有 upstream 时为 0）
  remote      TEXT,                          -- origin 地址，用来显示 GitHub 链接
  summary     TEXT,                          -- README 首段，列表页「这是什么」
  preview     TEXT,
  readme      TEXT,
  readme_hash TEXT,
  state       TEXT NOT NULL DEFAULT 'auto',  -- auto 收编 / candidate 候选 / dropped 引擎减掉
  state_locked INTEGER NOT NULL DEFAULT 0,   -- 人拍过板：重扫不许覆盖它的 state
  run_local   INTEGER NOT NULL DEFAULT 0,     -- 跑在本机：没有 SSH 服务器也算"部署在这"
  user_name   TEXT,
  user_status TEXT,
  user_note   TEXT,
  user_pinned INTEGER NOT NULL DEFAULT 0,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_active ON project(active);
CREATE INDEX IF NOT EXISTS idx_project_seen   ON project(last_seen DESC);

CREATE TABLE IF NOT EXISTS project_server (
  project_path TEXT NOT NULL REFERENCES project(path) ON DELETE CASCADE,
  server_id    INTEGER NOT NULL REFERENCES server(id) ON DELETE CASCADE,
  PRIMARY KEY (project_path, server_id)
);

CREATE TABLE IF NOT EXISTS metric (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  server_id INTEGER NOT NULL REFERENCES server(id) ON DELETE CASCADE,
  ts TEXT NOT NULL,
  online INTEGER NOT NULL,
  hostname TEXT, load1 REAL,
  os_name TEXT, kernel TEXT, uptime_text TEXT,
  mem_used INTEGER, mem_total INTEGER, mem_pct REAL,
  disk_used TEXT, disk_total TEXT, disk_pct REAL,
  containers TEXT, ports TEXT,
  port_json TEXT,                  -- 端口明细 JSON：[{port,addr,proc,scope}]
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_metric_srv ON metric(server_id, ts DESC);

CREATE TABLE IF NOT EXISTS expiry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  expire TEXT,
  renew INTEGER NOT NULL DEFAULT 0,
  note TEXT DEFAULT '',
  provider TEXT DEFAULT '',
  last_err TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  cfg TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notify_cfg (id INTEGER PRIMARY KEY CHECK (id=1), cfg TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_cfg     (id INTEGER PRIMARY KEY CHECK (id=1), cfg TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, action TEXT NOT NULL, detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS notify_log (key TEXT PRIMARY KEY, ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sched_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  ok INTEGER NOT NULL,
  error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sched_job ON sched_run(job_id, ts DESC);"""

# 版本 → 该版本要执行的语句列表。1 是基线（SCHEMA_SQL），后续从 2 开始。
MIGRATIONS: dict[int, list[str]] = {
    # v2：老服务器兼容开关（paramiko 对 group1-sha1 这类算法默认不开口）
    2: ["ALTER TABLE server ADD COLUMN legacy_kex INTEGER NOT NULL DEFAULT 0"],
    # v3：列表页要显示「这是什么 / 同步状态」，扫描时把这些一起算出来
    3: [
        "ALTER TABLE project ADD COLUMN dirty_n INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE project ADD COLUMN unpushed_n INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE project ADD COLUMN remote TEXT",
        "ALTER TABLE project ADD COLUMN summary TEXT",
    ],
    # v4：服务器详情要显示系统/内核/运行时长
    4: [
        "ALTER TABLE metric ADD COLUMN os_name TEXT",
        "ALTER TABLE metric ADD COLUMN kernel TEXT",
        "ALTER TABLE metric ADD COLUMN uptime_text TEXT",
    ],
    # v5：引擎判定分档。没有硬证据（.git / 清单 / README）的旧条目降级为候选，
    #     不再冒充"项目"占着列表；数据一条不删，改名和备注都还在。
    5: [
        "ALTER TABLE project ADD COLUMN state TEXT NOT NULL DEFAULT 'auto'",
        """UPDATE project SET state='candidate'
           WHERE is_git=0 AND user_name IS NULL AND user_pinned=0
             AND (name_src IS NULL OR name_src='dirname')""",
    ],
    # v6：人手动收编/剔掉之后，重扫不许把决定冲掉
    6: ["ALTER TABLE project ADD COLUMN state_locked INTEGER NOT NULL DEFAULT 0"],
    # v7：项目可以标「跑在本机」。本机跑的项目没有 SSH 服务器可挂，
    #     但仍然是"部署在哪"的一部分，不然这类项目永远显示「未关联」。
    7: ["ALTER TABLE project ADD COLUMN run_local INTEGER NOT NULL DEFAULT 0"],
    # v8：端口要能说清「谁在听、绑哪个地址」。只存端口号时，用户看到 :22
    #     会怀疑"我明明没开 22"，其实是 sshd 在听 —— 信息缺失造成的误判。
    8: ["ALTER TABLE metric ADD COLUMN port_json TEXT"],
}


def _integrity_ok(c: sqlite3.Connection) -> str:
    try:
        row = c.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as err:
        return f"无法读取：{err}"
    return (row[0] if row else "") or "空结果"


def init_db(path: str | None = None) -> None:
    """建库 / 校验 / 迁移。失败一律抛 DBError，绝不静默重建."""
    if path:
        configure(path)

    if config.is_network_path(_db_path):
        raise DBError(
            "data 目录在网络路径上，SQLite 的 WAL 需要本地磁盘。\n"
            f"请把 data 目录移到本地磁盘：{os.path.dirname(_db_path)}"
        )

    c = conn()
    state = _integrity_ok(c)
    if state.lower() != "ok":
        raise DBError(
            "数据库完整性检查没通过，已停止启动（不会覆盖你的数据）。\n"
            f"库文件：{_db_path}\n"
            f"检查结果：{state}\n"
            f"{_recover_hint()}"
        )

    c.executescript(SCHEMA_SQL)

    cur = get_meta("schema_version")
    if cur is None:
        # 全新库：先留一份空的基线备份，再写版本号。
        backup_now(tag="baseline")
        with tx() as t:
            set_meta("schema_version", str(SCHEMA_VERSION), t)
        return

    v = int(cur)
    if v > SCHEMA_VERSION:
        raise DBError(
            f"数据库版本（{v}）比当前程序（{SCHEMA_VERSION}）新，可能是用了更新的版本。\n"
            f"请换回新版程序，或先备份 {_db_path} 再处理。\n"
            f"{_recover_hint()}"
        )

    pending = sorted(n for n in MIGRATIONS if v < n <= SCHEMA_VERSION)
    if pending:
        # 迁移前先备份：迁移是写操作里唯一「不可撤销且最难回退」的一步
        # （改表结构）。出错能回滚，但「结构对了数据丢了」没法回滚。
        backup_now(tag="pre-migrate")
    for n in pending:
        try:
            with tx() as t:
                for stmt in MIGRATIONS[n]:
                    t.execute(stmt)
                set_meta("schema_version", str(n), t)
        except Exception as err:
            raise DBError(f"迁移到 schema_version={n} 失败，已回滚：{err}") from err
        v = n

    _maybe_startup_backup()


def _maybe_startup_backup() -> None:
    """启动时补一份快照：最近 12 小时已有备份就跳过.

    迁移只在「有新 MIGRATIONS」时才触发备份；老库不升级就等于永远没备份，
    而库里存着服务器凭据和项目清单 —— 所以启动时兜一层，12 小时最多一份。
    """
    latest = latest_backup()
    if latest:
        try:
            age_h = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(latest))).total_seconds() / 3600
        except OSError:
            age_h = 999.0
        if age_h < 12:
            return
    try:
        backup_now(tag="startup")
    except DBError as err:
        logging.getLogger("wb.db").warning("启动时自动备份失败（不影响启动）：%s", err)


# ------------------------------------------------------------------ audit
def audit(action: str, detail: str = "") -> None:
    """写操作留痕；失败不影响主流程，但必须进日志（不许静默）."""
    try:
        execute(
            "INSERT INTO audit(ts, action, detail) VALUES(?,?,?)",
            (now(), action, detail[:500]),
        )
    except sqlite3.Error as err:  # pragma: no cover - 只在磁盘满等极端情况触发
        import logging

        logging.getLogger("wb").warning("审计写入失败：%s（%s %s）", err, action, detail[:80])
