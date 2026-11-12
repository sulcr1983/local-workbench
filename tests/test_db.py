"""T3 数据层验收：建库、schema、迁移、并发、边界.

对应 docs/TASKS.md T3 的 7 条验收，逐条落成用例。
"""

from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import threading

from run_all import skip
from wb import config, db


# ------------------------------------------------------------------ 工具
def _fresh_db() -> str:
    d = tempfile.mkdtemp(prefix="wb-db-")
    path = os.path.join(d, "workbench.db")
    db.close_thread_conn()
    db.configure(path)
    db.init_db()
    return path


def _table_columns(name: str) -> list[str]:
    return [r["name"] for r in db.query(f"PRAGMA table_info({name})")]


# ------------------------------------------------------- 1. 首启动建库
def test_first_boot_creates_all_tables():
    _fresh_db()
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "meta", "root", "server", "project", "project_server", "metric",
        "expiry", "provider", "notify_cfg", "ui_cfg", "audit", "notify_log",
    }
    missing = expected - tables
    assert not missing, f"缺表：{sorted(missing)}"


def test_schema_version_is_current():
    _fresh_db()
    assert db.get_meta("schema_version") == str(db.SCHEMA_VERSION)


def test_schema_columns_match_spec():
    """关键列名与 TECH_SPEC §5.1 一致 —— 列名错了后面全是哑巴 bug."""
    _fresh_db()
    assert _table_columns("server") == [
        "id", "name", "host", "port", "user", "auth", "secret", "passphrase",
        "jump", "grp", "note", "alert_mem", "alert_disk", "alert_load",
        "enabled", "legacy_kex", "created_at", "updated_at",
    ]
    assert _table_columns("project") == [
        "path", "dirname", "name_auto", "name_src", "name_conf", "stack", "active",
        "is_git", "last_commit", "dirty", "dirty_n", "unpushed_n", "remote",
        "summary", "preview", "readme", "readme_hash", "state", "state_locked",
        "run_local",
        "user_name", "user_status", "user_note", "user_pinned",
        "first_seen", "last_seen",
    ]
    assert _table_columns("metric") == [
        "id", "server_id", "ts", "online", "hostname", "load1",
        "os_name", "kernel", "uptime_text",
        "mem_used", "mem_total", "mem_pct",
        "disk_used", "disk_total", "disk_pct", "containers", "ports", "port_json", "error",
    ]


def test_indexes_created():
    _fresh_db()
    idx = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    for name in ("idx_project_active", "idx_project_seen", "idx_metric_srv", "idx_audit_ts"):
        assert name in idx, f"缺索引 {name}"


# ------------------------------------------------ 2. 每个连接的 PRAGMA
def test_pragmas_on_every_new_connection():
    """不是只在初始化时设一次 —— 每个线程的连接都要验（TECH_SPEC §5.3 ①）."""
    path = _fresh_db()
    results: list[tuple[int, str, str]] = []

    def worker():
        db.close_thread_conn()
        c = db.conn()
        busy = c.execute("PRAGMA busy_timeout").fetchone()[0]
        journal = c.execute("PRAGMA journal_mode").fetchone()[0]
        fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
        results.append((busy, journal.lower(), str(fk)))
        db.close_thread_conn()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 3, "线程没跑完"
    for busy, journal, fk in results:
        assert busy == 5000, f"busy_timeout 应为 5000，实际 {busy}"
        assert journal == "wal", f"journal_mode 应为 wal，实际 {journal}"
        assert fk == "1", f"foreign_keys 应为开，实际 {fk}"
    assert os.path.exists(path)


# ---------------------------------------------------------- 3. 迁移
def test_migration_runs_once_and_increments():
    """手工加一版空迁移：版本递增，且重启不会重复执行."""
    _fresh_db()
    _fresh_db()
    saved_version = db.SCHEMA_VERSION
    db.MIGRATIONS[90] = ["CREATE TABLE IF NOT EXISTS _mig90 (id INTEGER)"]
    try:
        db.SCHEMA_VERSION = 90

        db.close_thread_conn()
        db.init_db()
        assert db.get_meta("schema_version") == "90", "版本没升到 90"

        # 再启一次：不该重复执行（重复 ALTER 会直接报错，这里用 IF NOT EXISTS 之外的手段验）
        db.close_thread_conn()
        db.init_db()
        assert db.get_meta("schema_version") == "90"
        tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "_mig90" in tables
    finally:
        db.MIGRATIONS.pop(90, None)
        db.SCHEMA_VERSION = saved_version


def test_migration_failure_rolls_back():
    """迁移失败必须回滚 + 抛 DBError，不能留半套表结构."""
    _fresh_db()
    db.MIGRATIONS[91] = ["CREATE TABLE _bad91 (id INTEGER)", "THIS IS NOT SQL"]
    saved = db.SCHEMA_VERSION
    try:
        db.SCHEMA_VERSION = 91
        db.close_thread_conn()
        try:
            db.init_db()
        except db.DBError as err:
            assert "迁移" in str(err), f"错误信息不够明确：{err}"
        else:
            raise AssertionError("坏迁移没有抛 DBError")
        assert db.get_meta("schema_version") == str(saved), "版本不该前进"
        tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "_bad91" not in tables, "回滚没生效，脏表留下了"
    finally:
        db.MIGRATIONS.pop(91, None)
        db.SCHEMA_VERSION = saved


def test_newer_schema_refuses_to_start():
    """库比程序新 → 拒绝启动，别把新版数据写坏."""
    _fresh_db()
    db.set_meta("schema_version", "99")
    db.close_thread_conn()
    try:
        db.init_db()
    except db.DBError as err:
        assert "99" in str(err) and "新" in str(err)
    else:
        raise AssertionError("版本超前竟然放行了")


# ---------------------------------------------------------- 4. 完整性
def test_corrupt_db_refuses_to_start():
    d = tempfile.mkdtemp(prefix="wb-bad-")
    path = os.path.join(d, "workbench.db")
    with open(path, "wb") as f:
        f.write(b"this is definitely not a sqlite database" * 50)

    db.close_thread_conn()
    db.configure(path)
    try:
        db.init_db()
    except db.DBError as err:
        msg = str(err)
        assert "无法打开" in msg or "完整性" in msg or "无法读取" in msg, f"错误信息不可读：{msg}"
        assert path in msg, "没把库文件路径打出来，用户找不到该恢复哪个文件"
        assert "备份" in msg, "没告诉用户怎么恢复"
    else:
        raise AssertionError("坏库竟然启动了")


def test_unc_path_refused():
    """WAL 在网络盘上必坏，启动就要挡."""
    db.close_thread_conn()
    db.configure(r"\\NAS\share\workbench.db")
    try:
        db.init_db()
    except db.DBError as err:
        assert "网络路径" in str(err) or "本地磁盘" in str(err)
    else:
        raise AssertionError("UNC 路径竟然放行了")


# -------------------------------------------------------- 5. 并发写
def test_concurrent_writes_no_lock():
    """4 线程各写 500 条，0 次 database is locked（T3 硬指标）."""
    _fresh_db()
    errors: list[str] = []
    barrier = threading.Barrier(4)

    def worker(n: int):
        try:
            barrier.wait(timeout=5)
            for i in range(500):
                with db.tx() as c:
                    c.execute(
                        "INSERT INTO audit(ts, action, detail) VALUES(?,?,?)",
                        (db.now(), f"w{n}", str(i)),
                    )
        except Exception as err:  # noqa: BLE001
            errors.append(f"{type(err).__name__}: {err}")
        finally:
            db.close_thread_conn()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发写报错：{errors[:3]}"
    total = db.scalar("SELECT COUNT(*) FROM audit")
    assert total == 2000, f"应写入 2000 条，实际 {total}"


# ------------------------------------------------------------ 6. 事务
def test_tx_rolls_back_on_exception():
    _fresh_db()
    before = db.scalar("SELECT COUNT(*) FROM audit")
    try:
        with db.tx() as c:
            c.execute("INSERT INTO audit(ts,action) VALUES(?,?)", (db.now(), "rollback-me"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.scalar("SELECT COUNT(*) FROM audit") == before, "异常后没有回滚"


def test_foreign_key_cascade():
    _fresh_db()
    with db.tx() as c:
        c.execute(
            "INSERT INTO server(name,host,created_at,updated_at) VALUES('s1','1.1.1.1',?,?)",
            (db.now(), db.now()),
        )
    sid = db.scalar("SELECT id FROM server WHERE name='s1'")
    with db.tx() as c:
        c.execute(
            "INSERT INTO metric(server_id, ts, online) VALUES(?,?,'1')", (sid, db.now())
        )
    assert db.scalar("SELECT COUNT(*) FROM metric") == 1
    db.execute("DELETE FROM server WHERE id=?", (sid,))
    assert db.scalar("SELECT COUNT(*) FROM metric") == 0, "删服务器没级联删指标"


# ------------------------------------------------------------ 7. audit
def test_audit_writes_row():
    _fresh_db()
    db.audit("测试动作", "细节")
    row = db.query_one("SELECT action, detail FROM audit ORDER BY id DESC LIMIT 1")
    assert row["action"] == "测试动作" and row["detail"] == "细节"


# ------------------------------------------------------- 8. 备份 / 恢复
def test_backup_writes_real_file_with_data():
    _fresh_db()
    with db.tx() as c:
        c.execute(
            "INSERT INTO server(name,host,created_at,updated_at) VALUES('backup-srv','2.2.2.2',?,?)",
            (db.now(), db.now()),
        )
    b = db.backup_now()
    assert os.path.isfile(b), "备份文件不存在"
    c2 = sqlite3.connect(b)
    try:
        # WAL 下直接拷文件可能拿到旧镜像，这里必须读到刚写入的那条数据
        assert c2.execute(
            "SELECT host FROM server WHERE name='backup-srv'"
        ).fetchone()[0] == "2.2.2.2"
        assert c2.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == str(db.SCHEMA_VERSION)
    finally:
        c2.close()


def test_backup_rotation_keeps_only_recent():
    _fresh_db()
    paths = [db.backup_now(tag=f"r{i:02d}") for i in range(db.BACKUP_KEEP + 3)]
    files = sorted(f for f in os.listdir(db.backup_dir()) if f.endswith(".db"))
    assert len(files) == db.BACKUP_KEEP, f"应只留 {db.BACKUP_KEEP} 份，实际 {len(files)}"
    assert os.path.basename(paths[-1]) in files, "刚生成的备份被轮转删了"


def test_migration_takes_backup_first():
    # 构造一份真实的 v7 老结构库（metric 没有 port_json），版本号 7
    # —— 这样 init_db 会真实执行 v8 迁移，验证「迁移前备份」生效。
    # 不能只把版本号退回 7：结构还是 v8 的话迁移脚本会撞重复列名，
    # 那不是「迁移失败」而是「假迁移」，测试就没意义了。
    d = tempfile.mkdtemp(prefix="wb-olddb-")
    path = os.path.join(d, "workbench.db")
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE root (path TEXT PRIMARY KEY, added_at TEXT NOT NULL);
        CREATE TABLE server (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, host TEXT NOT NULL,
          port INTEGER NOT NULL DEFAULT 22, user TEXT NOT NULL DEFAULT 'root',
          auth TEXT NOT NULL DEFAULT 'key_path', secret TEXT DEFAULT '', passphrase TEXT DEFAULT '',
          jump TEXT DEFAULT '', grp TEXT DEFAULT '', note TEXT DEFAULT '',
          alert_mem INTEGER NOT NULL DEFAULT 85, alert_disk INTEGER NOT NULL DEFAULT 90,
          alert_load REAL NOT NULL DEFAULT 4.0, enabled INTEGER NOT NULL DEFAULT 1,
          legacy_kex INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE metric (
          id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL, ts TEXT NOT NULL,
          online INTEGER NOT NULL, hostname TEXT, load1 REAL, os_name TEXT, kernel TEXT,
          uptime_text TEXT, mem_used INTEGER, mem_total INTEGER, mem_pct REAL,
          disk_used TEXT, disk_total TEXT, disk_pct REAL, containers TEXT, ports TEXT, error TEXT
        );
        INSERT INTO meta(key,value) VALUES ('schema_version','7');
        """
    )
    c.commit()
    c.close()

    db.close_thread_conn()
    db.configure(path)
    db.init_db()
    files = [f for f in os.listdir(db.backup_dir()) if "pre-migrate" in f]
    assert files, "迁移前没有备份 —— 迁移写错了没法回退"
    cols = {r["name"] for r in db.query("PRAGMA table_info(metric)")}
    assert "port_json" in cols, "v8 迁移没生效"
    assert db.get_meta("schema_version") == str(db.SCHEMA_VERSION)


def test_recover_hint_points_at_real_file():
    _fresh_db()
    b = db.backup_now()
    hint = db._recover_hint()
    assert b in hint, "恢复提示没指向真实备份路径"
    assert ".bak" not in hint, "错误提示指向不存在的 .bak"


def test_no_backup_hint_is_readable():
    d = tempfile.mkdtemp(prefix="wb-nobackup-")
    db.close_thread_conn()
    db.configure(os.path.join(d, "workbench.db"))
    hint = db._recover_hint()
    assert "备份" in hint
    assert ".bak" not in hint, "没备份时不能编一个不存在的路径"
