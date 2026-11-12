"""T9 / T10 服务器模块验收：列表、指标、离线态、告警、并发、节流、增删改."""

from __future__ import annotations

import os
import tempfile
import time

from _sshd import FakeSSHServer, SilentTCPServer
from wb import create_app, db, monitor, ssh
from wb.probe import Snapshot


def _setup(servers: int = 0):
    root = tempfile.mkdtemp(prefix="wb-srv-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)

    ts = db.now()
    with db.tx() as c:
        for i in range(1, servers + 1):
            c.execute(
                """INSERT INTO server(name, host, port, user, auth, secret,
                                      grp, note, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (f"机器 {i}", f"10.0.0.{i}", 22, "root", "password", "pw", "", "", ts, ts),
            )
    return app.test_client(), root


def _html(resp) -> str:
    return resp.get_data(as_text=True)


def _add_metric(sid: int, *, online=1, mem=30.0, disk=40.0, load=0.5, err="") -> None:
    with db.tx() as c:
        c.execute(
            """INSERT INTO metric(server_id, ts, online, hostname, load1, mem_used, mem_total,
                                  mem_pct, disk_used, disk_total, disk_pct, containers, ports, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, db.now(), online, "h1", load, 100, 1000, mem, "10G", "59G", disk, "a,b", ":22", err),
        )


# ---------------------------------------------------------------- 列表页
def test_empty_state():
    c, _ = _setup(0)
    body = _html(c.get("/servers"))
    assert "还没有服务器" in body
    assert "添加服务器" in body


def test_servers_page_lists_cards():
    c, _ = _setup(2)
    _add_metric(1)
    body = _html(c.get("/servers"))
    assert "机器 1" in body and "机器 2" in body
    assert "wb-srvcard" in body


def test_offline_card_shows_readable_reason():
    c, _ = _setup(1)
    _add_metric(1, online=0, err="连接超时（18s）：10.0.0.1:22 没有响应")
    body = _html(c.get("/servers"))
    # 采集失败用彩色状态标签 + 原因，不再用整块红字（5 张卡同一句红字是噪音）
    assert "采集失败" in body
    assert "连接超时" in body
    assert "is-off" in body


def test_never_collected_card_is_not_an_alarm():
    """从没连过 ≠ 采集失败，不该用错误样式吓人."""
    c, _ = _setup(1)
    body = _html(c.get("/servers"))
    assert "未采集" in body
    assert "采集失败" not in body


def test_alert_when_mem_over_threshold():
    c, _ = _setup(1)
    _add_metric(1, mem=92.0)
    body = _html(c.get("/servers"))
    assert "is-alert" in body
    assert "超阈值" in body
    assert "台超过阈值" in body


def test_pagination_11_servers():
    c, _ = _setup(11)
    body1 = _html(c.get("/servers"))
    # 用 data-srv 计数：class="wb-srvcard 会同时匹配 -hd / -bd 子元素
    assert body1.count("data-srv=") == 8
    assert "wb-pager" in body1
    body2 = _html(c.get("/servers?srv_page=2"))
    assert body2.count("data-srv=") == 3


def test_filter_offline():
    c, _ = _setup(2)
    _add_metric(1, online=1)
    _add_metric(2, online=0, err="x")
    body = _html(c.get("/servers?flt=offline"))
    assert "机器 2" in body and "机器 1" not in body


# ---------------------------------------------------------------- 详情页
def test_detail_page_renders():
    c, _ = _setup(1)
    _add_metric(1, mem=42.0, disk=55.0)
    resp = c.get("/server/1")
    assert resp.status_code == 200
    body = _html(resp)
    assert "机器 1" in body and "内存" in body and "磁盘" in body


def test_detail_404_for_missing():
    c, _ = _setup(0)
    assert c.get("/server/999").status_code == 404


# --------------------------------------------------------- 节流与并发
def test_store_throttles_unchanged_values():
    _setup(1)
    snap = Snapshot(mem_pct=30.0, disk_pct=40.0, load1=0.5)
    r = {"server_id": 1, "online": 1, "snap": snap, "error": ""}

    monitor.store([r])
    monitor.store([r])
    monitor.store([r])
    assert db.scalar("SELECT COUNT(*) FROM metric") == 1, "值没变还写了三条"


def test_store_writes_when_value_changes():
    _setup(1)
    monitor.store([{"server_id": 1, "online": 1, "snap": Snapshot(mem_pct=30.0, disk_pct=40.0, load1=0.5), "error": ""}])
    monitor.store([{"server_id": 1, "online": 1, "snap": Snapshot(mem_pct=55.0, disk_pct=40.0, load1=0.5), "error": ""}])
    assert db.scalar("SELECT COUNT(*) FROM metric") == 2


def test_store_writes_when_online_state_flips():
    _setup(1)
    monitor.store([{"server_id": 1, "online": 1, "snap": Snapshot(mem_pct=30.0), "error": ""}])
    monitor.store([{"server_id": 1, "online": 0, "snap": None, "error": "超时"}])
    assert db.scalar("SELECT COUNT(*) FROM metric") == 2


def test_collect_all_runs_concurrently():
    """3 台超时 + 1 台正常：并发应 ≈ 单台超时，不是三倍."""
    good = FakeSSHServer(user="tester", password="pw").start()
    silent = [SilentTCPServer() for _ in range(3)]
    old = ssh.TIMEOUT
    ssh.TIMEOUT = 2
    try:
        servers = [
            {"id": 1, "host": "127.0.0.1", "port": good.port, "user": "tester", "auth": "password", "secret": "pw"},
        ] + [
            {"id": i + 2, "host": "127.0.0.1", "port": s.port, "user": "tester", "auth": "password", "secret": "pw"}
            for i, s in enumerate(silent)
        ]
        t0 = time.time()
        results = monitor.collect_all(servers, workers=4)
        cost = time.time() - t0
        assert len(results) == 4
        assert results[0]["online"] == 1
        assert cost < 5, f"4 台并发采集用了 {cost:.1f}s，事务/并发没生效"
    finally:
        ssh.TIMEOUT = old
        good.stop()
        for s in silent:
            s.stop()


def test_collect_one_records_offline_reason():
    r = monitor.collect_one(
        {"id": 9, "host": "127.0.0.1", "port": 1, "user": "x", "auth": "password", "secret": "y"}
    )
    assert r["online"] == 0
    assert r["error"], "离线必须带原因"


# ------------------------------------------------------------ 增删改
def test_add_server_via_api():
    c, _ = _setup(0)
    resp = c.post(
        "/api/server/save",
        data={"name": "新机器", "host": "1.2.3.4", "port": "22", "user": "root",
              "auth": "password", "secret": "pw"},
    )
    assert resp.get_json()["ok"] is True
    assert db.scalar("SELECT COUNT(*) FROM server") == 1


def test_add_server_rejects_duplicate_name():
    c, _ = _setup(1)
    resp = c.post(
        "/api/server/save",
        data={"name": "机器 1", "host": "9.9.9.9", "port": "22", "user": "root",
              "auth": "password", "secret": "pw"},
    )
    assert resp.status_code == 400
    assert "已经有叫" in resp.get_json()["error"]


def test_add_server_requires_secret():
    c, _ = _setup(0)
    resp = c.post(
        "/api/server/save",
        data={"name": "x", "host": "1.1.1.1", "port": "22", "user": "root", "auth": "password", "secret": ""},
    )
    assert resp.status_code == 400
    assert "必须填" in resp.get_json()["error"]


def test_edit_keeps_secret_when_blank():
    c, _ = _setup(1)
    c.post("/api/server/save", data={"id": "1", "name": "机器 1", "host": "10.0.0.9",
                                     "port": "22", "user": "root", "auth": "password", "secret": ""})
    assert db.scalar("SELECT secret FROM server WHERE id=1") == "pw", "留空不该把密码清掉"
    assert db.scalar("SELECT host FROM server WHERE id=1") == "10.0.0.9"


def test_delete_blocked_when_projects_linked():
    c, root = _setup(1)
    ts = db.now()
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO project(path, dirname, name_auto, first_seen, last_seen)
               VALUES(?,?,?,?,?)""",
            (os.path.join(root, "p"), "p", "P", ts, ts),
        )
        conn.execute("INSERT INTO project_server(project_path, server_id) VALUES(?,1)", (os.path.join(root, "p"),))

    resp = c.post("/api/server/del", data={"id": "1"})
    assert resp.status_code == 400
    assert "关联" in resp.get_json()["error"]
    assert db.scalar("SELECT COUNT(*) FROM server") == 1


def test_delete_without_links_works():
    c, _ = _setup(1)
    assert c.post("/api/server/del", data={"id": "1"}).get_json()["ok"] is True
    assert db.scalar("SELECT COUNT(*) FROM server") == 0


def test_test_connection_success_uses_real_ssh():
    ok_srv = FakeSSHServer(user="tester", password="pw").start()
    c, _ = _setup(0)
    try:
        resp = c.post(
            "/api/server/test",
            data={"name": "t", "host": "127.0.0.1", "port": str(ok_srv.port), "user": "tester",
                  "auth": "password", "secret": "pw"},
        )
        payload = resp.get_json()
        assert payload["ok"] is True, payload
        assert payload["hostname"] == "wb-test-host"
    finally:
        ok_srv.stop()


def test_test_connection_failure_is_chinese():
    ok_srv = FakeSSHServer(user="tester", password="pw").start()
    c, _ = _setup(0)
    try:
        resp = c.post(
            "/api/server/test",
            data={"name": "t", "host": "127.0.0.1", "port": str(ok_srv.port), "user": "tester",
                  "auth": "password", "secret": "wrong"},
        )
        payload = resp.get_json()
        assert payload["ok"] is False
        assert "认证失败" in payload["error"]
        assert db.scalar("SELECT COUNT(*) FROM server") == 0, "验证失败不该入库"
    finally:
        ok_srv.stop()


# ---------------------------------------------------------------- 详情页
def test_detail_page_renders_with_ports():
    """详情页必须真渲染出来 —— 之前 g.items 撞了 dict.items 方法，整页 500 而测试没抓到."""
    c, _ = _setup(1)
    _add_metric(1)
    resp = c.get("/server/1")
    assert resp.status_code == 200, f"详情页崩了：{resp.status_code}"
    body = _html(resp)
    assert "监听端口" in body
    assert "基本信息" in body


def test_detail_groups_ports_by_range():
    """387 个端口平铺会把页面撑成数字墙 —— 必须分组折叠."""
    c, _ = _setup(1)
    with db.tx() as tx:
        tx.execute(
            """INSERT INTO metric(server_id, ts, online, hostname, load1, mem_used, mem_total,
                                  mem_pct, disk_used, disk_total, disk_pct, containers, ports, error)
               VALUES(?,?,1,'h1',0.5,100,1000,30.0,'10G','59G',40.0,'',?,NULL)""",
            (1, db.now(), ":22,:80,:443,:6010,:6011,:40000,:50000"),
        )
    body = _html(c.get("/server/1"))
    # 常用的单拎出来
    assert "wb-port is-common" in body
    assert "SSH" in body
    # 其余按区间折叠，而不是全平铺
    assert "常见服务（1024–9999）" in body
    assert "动态端口（32768+）" in body


def test_detail_without_metric_shows_offline_reason():
    c, _ = _setup(1)
    body = _html(c.get("/server/1"))
    assert "无法采集" in body or "还没采集过" in body


def test_group_ports_pure_function():
    from wb.blueprints.servers import group_ports

    r = group_ports([":22", ":80", ":6010", ":6020", ":40000", "bad", ":3306"])
    # common 现在带进程信息，不再是纯端口号
    assert [x["port"] for x in r["common"]] == [22, 80, 3306]
    assert all(x["proc"] == [] and x["scope"] == "" for x in r["common"]), "没明细时不该瞎猜进程"
    assert r["has_detail"] is False
    assert r["total"] == 6, "非法值要被丢掉"
    labels = [g["label"] for g in r["groups"]]
    assert "常见服务（1024–9999）" in labels
    assert "动态端口（32768+）" in labels


def test_group_ports_attaches_process_and_scope():
    """端口要能说清「谁在听、对外还是只对本机」—— 只给端口号会让人误判。"""
    from wb.blueprints.servers import group_ports

    details = [
        {"port": 22, "addr": "0.0.0.0", "proc": "sshd", "scope": "all"},
        {"port": 80, "addr": "127.0.0.1", "proc": "nginx", "scope": "local"},
    ]
    r = group_ports([":22", ":80"], details)
    info = {x["port"]: x for x in r["common"]}
    assert info[22]["proc"] == ["sshd"] and info[22]["scope"] == "all"
    assert info[80]["scope"] == "local", "127.x 绑定的不该算对外可达"
    assert r["has_detail"] is True


def test_group_ports_detail_covers_folded_ports_too():
    """折叠区里的端口也要能拿到进程名，不能只有常用端口有。"""
    from wb.blueprints.servers import group_ports

    details = [{"port": 8766, "addr": "0.0.0.0", "proc": "python3", "scope": "all"}]
    r = group_ports([":8766"], details)
    assert r["detail"][8766]["proc"] == ["python3"]
    assert r["detail"][8766]["scope"] == "all"


# ---------------------------------------------------------------- 离线重连
def test_reconnect_api_reports_ok(monkeypatch=None):
    c, _ = _setup(1)
    from unittest.mock import patch

    from wb import monitor

    ok_result = {"server_id": 1, "online": 1, "snap": None, "error": ""}
    with patch.object(monitor, "collect_one", lambda srv: ok_result), \
         patch.object(monitor, "store", lambda results: {"written": 1, "skipped": 0}):
        resp = c.post("/api/server/reconnect", data={"id": "1"})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True and body["online"] is True


def test_reconnect_api_passes_reason_through():
    """失败原因要原样回给页面，不能只给一个 false 让人猜."""
    c, _ = _setup(1)
    from unittest.mock import patch

    from wb import monitor

    bad = {"server_id": 1, "online": 0, "snap": None, "error": "连接超时（18s）：10.0.0.1:22 没有响应。"}
    with patch.object(monitor, "collect_one", lambda srv: bad), \
         patch.object(monitor, "store", lambda results: {"written": 0, "skipped": 1}):
        body = c.post("/api/server/reconnect", data={"id": "1"}).get_json()
    assert body["online"] is False
    assert "连接超时" in body["error"]


def test_reconnect_unknown_id_returns_404():
    c, _ = _setup(0)
    resp = c.post("/api/server/reconnect", data={"id": "999"})
    assert resp.status_code == 404


def test_collect_auto_retries_with_legacy_kex():
    """握手谈不拢时系统自己降级重试一次，通了就把 legacy_kex 记住 —— 不该让人去开开关."""
    _setup(1)
    from unittest.mock import patch

    from wb import monitor, ssh

    seen = []

    def fake_open(server, jump="", timeout=None):
        legacy = bool(server.get("legacy_kex"))
        seen.append(legacy)
        if not legacy:
            raise ssh.SSHError("10.0.0.1 提出的密钥交换算法和本机没有交集 —— 握手谈不拢")
        return "client"

    with patch.object(ssh, "open_session", fake_open), \
         patch.object(ssh, "close", lambda c: None), \
         patch.object(ssh, "run", lambda c, cmd, timeout=None: ("", "", 1)):
        result = monitor.collect_one({"id": 1, "host": "10.0.0.1", "port": 22,
                                      "user": "root", "auth": "password", "secret": "pw"})

    assert seen == [False, True], "应该先普通连、再兼容连"
    # 重试这次 run 返回码 1 且无输出 → 记为失败，但降级动作本身发生了
    assert result["online"] == 0
    assert "兼容" in result["error"]


def test_auto_legacy_is_remembered_after_success():
    _setup(1)
    from unittest.mock import patch

    from wb import monitor, ssh

    def fake_open(server, jump="", timeout=None):
        if not server.get("legacy_kex"):
            raise ssh.SSHError("握手谈不拢")
        return "client"

    with patch.object(ssh, "open_session", fake_open), \
         patch.object(ssh, "close", lambda c: None), \
         patch.object(ssh, "run", lambda c, cmd, timeout=None: ("ok", "", 0)), \
         patch.object(monitor.probe, "parse", lambda out: None):
        result = monitor.collect_one({"id": 1, "host": "10.0.0.1", "port": 22,
                                      "user": "root", "auth": "password", "secret": "pw"})

    assert result["online"] == 1
    assert "自动切到兼容模式" in result.get("auto_fixed", "")
    assert db.scalar("SELECT legacy_kex FROM server WHERE id=1") == 1, "要把这个设置记住，下次别再试错"


def test_non_kex_failure_does_not_retry():
    """密码错了不该无谓重试 —— 只对握手问题降级."""
    _setup(1)
    from unittest.mock import patch

    from wb import monitor, ssh

    seen = []

    def fake_open(server, jump="", timeout=None):
        seen.append(bool(server.get("legacy_kex")))
        raise ssh.SSHError("认证失败：拒绝了这组用户名/密码")

    with patch.object(ssh, "open_session", fake_open), \
         patch.object(ssh, "close", lambda c: None):
        monitor.collect_one({"id": 1, "host": "10.0.0.1", "port": 22,
                             "user": "root", "auth": "password", "secret": "pw"})

    assert seen == [False], "认证失败不该重试"


# ---------------------------------------------------------------- 卡片交互（v7）
def test_card_title_is_the_detail_link_and_detail_button_is_gone():
    """点标题行进详情 —— 原来那个「详情」按钮该消失，别留两个入口."""
    c, _ = _setup(1)
    _add_metric(1)
    body = _html(c.get("/servers"))
    assert "wb-srvlink" in body, "标题行得是链接"
    assert 'href="/server/1"' in body, "链接要指到详情页"
    assert ">详情</a>" not in body, "「详情」按钮应该没了"
    assert "data-del-srv" in body, "要有删除入口"


def test_card_shows_note_under_host():
    c, _ = _setup(1)
    with db.tx() as t:
        t.execute("UPDATE server SET note='跑着热点爬虫' WHERE id=1")
    body = _html(c.get("/servers"))
    assert "跑着热点爬虫" in body
    assert "wb-srvnote" in body


def test_delete_with_links_needs_confirm_then_cascades():
    """连不上的死机器往往还挂着项目 —— 不能因为有关联就永远删不掉."""
    c, _ = _setup(1)
    with db.tx() as t:
        t.execute("INSERT INTO project(path, dirname, first_seen, last_seen) VALUES('D:/p1','p1',?,?)", (db.now(), db.now()))
        t.execute("INSERT INTO project_server(project_path, server_id) VALUES('D:/p1', 1)")

    resp = c.post("/api/server/del", data={"id": "1"})
    assert resp.status_code == 400, "有关联要先确认"
    j = resp.get_json()
    assert j["need_confirm"] is True and j["refs"] == 1

    resp = c.post("/api/server/del", data={"id": "1", "force": "1"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM server", default=0) == 0
    assert db.scalar("SELECT COUNT(*) FROM project_server", default=0) == 0, "关联要一起断"


def test_delete_removes_metrics_too():
    c, _ = _setup(1)
    _add_metric(1)
    assert db.scalar("SELECT COUNT(*) FROM metric", default=0) == 1
    c.post("/api/server/del", data={"id": "1"})
    assert db.scalar("SELECT COUNT(*) FROM metric", default=0) == 0


def test_server_detail_lets_you_pick_projects():
    """服务器详情页也能反向勾项目 —— 不是只能从项目那边挂过来."""
    c, _ = _setup(1)
    with db.tx() as t:
        t.execute("INSERT INTO project(path, dirname, name_auto, first_seen, last_seen) VALUES('D:/p1','p1','项目一',?,?)", (db.now(), db.now()))
        t.execute("INSERT INTO project(path, dirname, name_auto, first_seen, last_seen) VALUES('D:/p2','p2','项目二',?,?)", (db.now(), db.now()))
    body = _html(c.get("/server/1"))
    assert "wb-projpicker" in body
    assert 'data-link-proj="D:/p1"' in body and 'data-link-proj="D:/p2"' in body
    assert "关联项目" in body


def test_server_detail_edit_button_opens_real_modal():
    """详情页「编辑」曾经只有 data-srv-edit，全站没有任何 JS 绑定 —— 点了没反应（假按钮）。
    现在必须走通用弹窗：按钮带 mbx-open，弹窗模板真的被引入，server 数据也在页面上。"""
    c, _ = _setup(1)
    body = _html(c.get("/server/1"))
    assert 'data-mbx-open="mbx-server"' in body
    assert 'data-mode="edit"' in body
    assert "data-srv=" in body, "弹窗要靠 closest('[data-srv]') 取数据"
    assert 'id="mbx-server"' in body, "编辑弹窗模板没被引入，按钮点了还是没反应"
    assert "data-srv-edit" not in body, "旧的死属性不该再出现"


def test_server_link_api_toggles_both_ways():
    c, _ = _setup(1)
    with db.tx() as t:
        t.execute("INSERT INTO project(path, dirname, first_seen, last_seen) VALUES('D:/p1','p1',?,?)", (db.now(), db.now()))

    r = c.post("/api/server/link", data={"id": "1", "path": "D:/p1", "on": "1"})
    assert r.status_code == 200 and r.get_json()["linked"] is True
    assert db.scalar("SELECT COUNT(*) FROM project_server", default=0) == 1

    r = c.post("/api/server/link", data={"id": "1", "path": "D:/p1", "on": "0"})
    assert r.status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM project_server", default=0) == 0


def test_server_link_api_rejects_unknown_project():
    c, _ = _setup(1)
    r = c.post("/api/server/link", data={"id": "1", "path": "D:/nope", "on": "1"})
    assert r.status_code == 404
    assert "不在库里" in r.get_json()["error"]


def test_project_run_local_toggle():
    """项目可以标「跑在本机」 —— 本机项目没有 SSH 服务器可挂，但也得能回答"部署在哪"."""
    c, _ = _setup(0)
    root_dir = tempfile.mkdtemp(prefix="wb-local-")
    proj = os.path.join(root_dir, "p1")
    with db.tx() as t:
        t.execute("INSERT INTO root(path, added_at) VALUES(?,?)", (root_dir, db.now()))
        t.execute(
            "INSERT INTO project(path, dirname, first_seen, last_seen) VALUES(?,?,?,?)",
            (proj, "p1", db.now(), db.now()),
        )
    r = c.post("/api/project/local", data={"rel": "p1", "on": "1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert db.scalar("SELECT run_local FROM project WHERE path=?", (proj,)) == 1
    r = c.post("/api/project/local", data={"rel": "p1", "on": "0"})
    assert db.scalar("SELECT run_local FROM project WHERE path=?", (proj,)) == 0
