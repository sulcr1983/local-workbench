"""一键发现 / 批量导入验收.

原则：系统该自动做的必须真做到（扫、去重、填好），不做假动作。
网络部分全部打桩 —— 测试不依赖外网，也不依赖本机真有服务器。
"""

from __future__ import annotations

import os
import socket
import tempfile
from unittest.mock import patch

from _sshd import FakeSSHServer
from wb import bootstrap, create_app, db


def _setup():
    root = tempfile.mkdtemp(prefix="wb-boot-")
    db.close_thread_conn()
    db.configure(os.path.join(root, "workbench.db"))
    db.init_db()
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client(), root


# ---------------------------------------------------------------- 端口探测
def test_tcp_alive_true_on_open_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert bootstrap.tcp_alive("127.0.0.1", port, timeout=1.0) is True
    finally:
        srv.close()


def test_tcp_alive_false_on_closed_port():
    # 先占一个端口再关掉，保证这个端口确实没人听
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    assert bootstrap.tcp_alive("127.0.0.1", port, timeout=0.5) is False


def test_tcp_alive_false_on_empty_host():
    assert bootstrap.tcp_alive("", 22) is False


# ---------------------------------------------------------------- 试连
def test_ssh_probe_without_key_does_not_raise():
    ok, why = bootstrap.ssh_probe("127.0.0.1", 22, "root", "/definitely/not/here")
    assert ok is False
    assert "私钥" in why


# ---------------------------------------------------------------- 汇总
def _fake_ssh(*_a, **_k):
    return {
        "config": [
            {"alias": "prod", "host": "10.0.0.5", "user": "deploy", "port": "2222",
             "key": "C:/k/id_ed25519"},
        ],
        "keys": [],
        "known_hosts": ["10.0.0.5", "10.0.0.6"],
        "ssh_dir": "C:/ssh",
    }


def test_gather_dedupes_same_host_port():
    """SSH 配置和 known_hosts 提到同一台，只该出现一次，且保留带 key 的那份."""
    _setup()
    with patch.object(bootstrap.discover, "ssh_candidates", _fake_ssh), \
         patch.object(bootstrap, "tcp_alive", lambda *a, **k: True), \
         patch.object(bootstrap, "ssh_probe", lambda *a, **k: (True, "prod-01")), \
         patch.object(bootstrap, "_from_cloud", lambda: ([], [])), \
         patch.object(bootstrap, "_local_keys", lambda: ["C:/k/id_ed25519"]):
        out = bootstrap.gather([], probe=True, verify=True)

    hosts = [(i["host"], i["port"]) for i in out["items"]]
    assert hosts.count(("10.0.0.5", 2222)) == 1, hosts
    item = next(i for i in out["items"] if i["host"] == "10.0.0.5")
    assert item["user"] == "deploy"
    assert item["verified"] is True
    assert item["status"] == "ready"
    assert item["hostname"] == "prod-01"


def test_gather_marks_unreachable_as_dead():
    _setup()
    with patch.object(bootstrap.discover, "ssh_candidates", _fake_ssh), \
         patch.object(bootstrap, "tcp_alive", lambda *a, **k: False), \
         patch.object(bootstrap, "_from_cloud", lambda: ([], [])), \
         patch.object(bootstrap, "_local_keys", lambda: []):
        out = bootstrap.gather([], probe=True, verify=False)
    assert all(i["status"] == "dead" for i in out["items"])
    assert out["stats"]["dead"] == len(out["items"])


def test_gather_marks_existing_servers():
    """已经在列表里的不该出现在可导入项里."""
    _setup()
    with db.tx() as c:
        c.execute(
            "INSERT INTO server(name,host,port,user,auth,secret,created_at,updated_at) "
            "VALUES('old','10.0.0.6',22,'root','password','x',?,?)",
            (db.now(), db.now()),
        )
    with patch.object(bootstrap.discover, "ssh_candidates", _fake_ssh), \
         patch.object(bootstrap, "tcp_alive", lambda *a, **k: True), \
         patch.object(bootstrap, "ssh_probe", lambda *a, **k: (False, "认证失败")), \
         patch.object(bootstrap, "_from_cloud", lambda: ([], [])), \
         patch.object(bootstrap, "_local_keys", lambda: ["C:/k/id_ed25519"]):
        out = bootstrap.gather([], probe=True, verify=True)

    item = next(i for i in out["items"] if i["host"] == "10.0.0.6")
    assert item["exists"] is True
    assert item["status"] == "exists"
    assert out["stats"]["exists"] == 1


def test_gather_survives_cloud_failure():
    """云账号配错了不能把整个扫描搞挂，要作为 warning 带回去."""
    _setup()
    with patch.object(bootstrap.discover, "ssh_candidates", lambda: {"config": [], "keys": [], "known_hosts": []}), \
         patch.object(bootstrap, "_from_cloud", lambda: ([], ["腾讯云：SecretId 错误"])), \
         patch.object(bootstrap, "_local_keys", lambda: []):
        out = bootstrap.gather([], probe=False, verify=False)
    assert out["items"] == []
    assert any("腾讯云" in w for w in out["warnings"])


# ---------------------------------------------------------------- 导入
def test_import_inserts_and_skips_duplicates():
    _setup()
    items = [
        {"name": "A", "host": "1.2.3.4", "port": 22, "user": "root", "auth": "key_path",
         "secret": "C:/k/id_ed25519"},
        {"name": "A", "host": "1.2.3.4", "port": 22, "user": "root", "auth": "key_path",
         "secret": "C:/k/id_ed25519"},
        {"name": "B", "host": "5.6.7.8", "port": 2200, "user": "ubuntu", "auth": "password",
         "secret": "pw"},
    ]
    res = bootstrap.import_items(items)
    assert res["added"] == 2 and res["skipped"] == 1
    assert db.scalar("SELECT COUNT(*) FROM server") == 2


def test_import_marks_missing_password():
    """密码认证但没密码 —— 不能假装能连，要标出来让人知道要补."""
    _setup()
    bootstrap.import_items([{"name": "C", "host": "9.9.9.9", "port": 22,
                             "user": "root", "auth": "password", "secret": ""}])
    note = db.scalar("SELECT note FROM server WHERE host='9.9.9.9'")
    assert "待补密码" in (note or "")


def test_import_rejects_empty_host():
    _setup()
    res = bootstrap.import_items([{"name": "X", "host": "", "port": 22}])
    assert res["added"] == 0 and res["skipped"] == 1


def test_import_ignores_unknown_fields():
    """前端传什么都不能让它写进别的列 —— 只收白名单."""
    _setup()
    bootstrap.import_items([{"name": "D", "host": "8.8.8.8", "port": 22,
                             "user": "root", "auth": "password", "secret": "pw",
                             "alert_mem": 1, "jump": "evil"}])
    row = db.query_one("SELECT alert_mem, jump FROM server WHERE host='8.8.8.8'")
    assert row["jump"] in (None, ""), "白名单外的字段不该被写进去"


# ---------------------------------------------------------------- 接口
def test_discover_api_returns_json():
    c, _ = _setup()
    with patch.object(bootstrap.discover, "ssh_candidates", _fake_ssh), \
         patch.object(bootstrap, "tcp_alive", lambda *a, **k: True), \
         patch.object(bootstrap, "ssh_probe", lambda *a, **k: (True, "h")), \
         patch.object(bootstrap, "_from_cloud", lambda: ([], [])), \
         patch.object(bootstrap, "_local_keys", lambda: ["C:/k/id_ed25519"]):
        resp = c.post("/api/servers/discover")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and "items" in body and "stats" in body


def test_import_api_rejects_bad_payload():
    c, _ = _setup()
    assert c.post("/api/servers/import", data={"items": "not json"}).status_code == 400
    assert c.post("/api/servers/import", data={"items": "[]"}).status_code == 400


def test_import_api_works_end_to_end():
    c, _ = _setup()
    payload = ('[{"name":"E","host":"7.7.7.7","port":22,"user":"root",'
               '"auth":"password","secret":"pw"}]')
    resp = c.post("/api/servers/import", data={"items": payload})
    assert resp.status_code == 200
    assert resp.get_json()["added"] == 1
    assert "7.7.7.7" in c.get("/servers").get_data(as_text=True)


# ---------------------------------------------------------------- 密钥解析
def test_ssh_config_keeps_all_identity_files():
    """一个 Host 可以写多个 IdentityFile（主密钥 + 兜底），不能只留最后一个.

    203.0.113.10 就是这样：新主密钥在前，旧兜底在后。只取最后一个 → 永远连不上。
    """
    text = (
        "Host prod\n"
        "  HostName 1.2.3.4\n"
        "  User root\n"
        "  IdentityFile ~/.ssh/id_new\n"
        "  IdentityFile ~/.ssh/id_old\n"
    )
    blocks = bootstrap.discover._ssh_config_blocks(text)
    assert blocks, "没解析出主机"
    files = blocks[0]["identity_files"]
    assert len(files) == 2, files
    assert files[0].endswith("id_new") and files[1].endswith("id_old")


def test_local_keys_detects_key_with_arbitrary_name():
    """私钥不一定叫 id_xxx —— 生产机器上用的是自定义文件名."""
    import tempfile

    d = tempfile.mkdtemp(prefix="wb-ssh-")
    # 故意拆成两段拼接：整串字面量会被各类 secret scanner 当成真私钥误报
    body = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
    for name, content in (
        ("deploy_key", body), ("id_ed25519", body),
        ("id_ed25519.pub", "ssh-ed25519 AAAA"),
        ("config", "Host x\n"), ("known_hosts", "1.2.3.4 ssh-ed25519 AAAA"),
    ):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(content)

    with patch.object(bootstrap.discover, "SSH_DIR", d):
        keys = bootstrap._local_keys()

    names = {os.path.basename(k) for k in keys}
    assert names == {"deploy_key", "id_ed25519"}, names


def test_split_keys_keeps_all_and_batches_by_max_auth_tries():
    """一次带太多密钥会撞 MaxAuthTries，但也不能截断 —— 截断会把老密钥永久藏起来."""
    keys = [f"/k{i}" for i in range(7)]
    with patch.object(bootstrap.os.path, "isfile", lambda p: True):
        got = bootstrap._split_keys("\n".join(keys))
    assert got == keys, "顺序和数量都要原样保留"
    batches = bootstrap._key_batches(got)
    assert [len(b) for b in batches] == [bootstrap.MAX_KEYS_PER_TRY, 2]
    assert sum(batches, []) == got, "分批不能丢也不能重排"


def test_local_keys_newest_first():
    """用户要的：密钥用最新的。刚生成的钥匙排前面，老的留在后面等着."""
    import tempfile
    import time as _t

    d = tempfile.mkdtemp(prefix="wb-sshk-")
    # 同上：拆开写，避免被 secret scanner 误判为真私钥
    body = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
    old, new = os.path.join(d, "aaa_old"), os.path.join(d, "zzz_new")
    for p in (old, new):
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
    # 显式压时间戳，别指望文件系统写入顺序
    _t.sleep(0.05)
    os.utime(old, (_t.time() - 86400 * 30, _t.time() - 86400 * 30))

    with patch.object(bootstrap.discover, "SSH_DIR", d):
        keys = bootstrap._local_keys()

    assert [os.path.basename(k) for k in keys] == ["zzz_new", "aaa_old"], keys


def test_ssh_probe_without_any_real_key():
    ok, why = bootstrap.ssh_probe("127.0.0.1", 22, "root", "/nope/1\n/nope/2")
    assert ok is False
    assert "私钥" in why


def test_ssh_probe_falls_through_to_next_key_batch():
    """第 1 批 5 把都不中、第 6 把才中 —— 不能因为 MaxAuthTries 就放弃后面的密钥."""
    tried: list[list[str]] = []

    def fake_try(host, port, user, keys, legacy):
        tried.append(list(keys))
        return (True, "hit") if "/k5" in keys else (False, "Authentication failed.")

    with patch.object(bootstrap.os.path, "isfile", lambda p: True), \
         patch.object(bootstrap, "_ssh_try", fake_try):
        ok, info = bootstrap.ssh_probe("1.2.3.4", 22, "root", "\n".join(f"/k{i}" for i in range(7)))

    assert ok is True and info == "hit"
    assert len(tried) == 2, tried
    assert tried[1][-1] == "/k6" and "/k5" in tried[1]


def test_ssh_probe_does_not_repeat_batches_on_handshake_error():
    """握手谈不拢跟密钥无关，别按批重复试 —— 只该常规一次 + legacy 一次."""
    from wb import ssh as sshmod

    calls: list[bool] = []

    def fake_try(host, port, user, keys, legacy):
        calls.append(legacy)
        return False, "Unable to agree on a key exchange (kex) algorithm"

    with patch.object(bootstrap.os.path, "isfile", lambda p: True), \
         patch.object(bootstrap, "_ssh_try", fake_try), \
         patch.object(sshmod, "cli_available", lambda: ""):
        ok, _info = bootstrap.ssh_probe("1.2.3.4", 22, "root", "\n".join(f"/k{i}" for i in range(7)))

    assert ok is False
    assert calls == [False, True], calls


# ---------------------------------------------------------------- 系统 ssh 后备
def test_cli_available_returns_path_or_empty():
    from wb import ssh

    path = ssh.cli_available()
    assert path == "" or os.path.isfile(path)


def test_run_via_cli_password_auth_uses_askpass():
    """密码认证也能走系统 ssh —— SSH_ASKPASS 顶上，不需要 sshpass 也不用 plink.

    密码故意带 cmd 特殊字符（& < > | ^ %），验证 .bat 用 type 读文件而不是 echo 拼接。
    """
    from wb import ssh

    assert ssh.cli_available(), "本机得有 ssh 客户端，这条兜底才有意义"
    pw = 'p&a<s>s|w^o"rd%PATH%1'
    srv = FakeSSHServer(user="tester", password=pw).start()
    try:
        out, _err, code = ssh.run_via_cli(
            {"host": "127.0.0.1", "port": srv.port, "user": "tester",
             "auth": "password", "secret": pw},
            "hostname", timeout=20,
        )
    finally:
        srv.stop()

    assert code == 0, out
    assert "wb-test-host" in out


def test_run_via_cli_rejects_password_without_secret():
    from wb import ssh

    try:
        ssh.run_via_cli({"host": "1.2.3.4", "port": 22, "user": "root",
                         "auth": "password", "secret": ""}, "hostname")
    except ssh.SSHError as err:
        assert "没填密码" in str(err)
    else:
        raise AssertionError("空密码不该发起连接")


def test_run_via_cli_key_auth_connects():
    """密钥路径也要真连一次 —— 参数拼错过（KnownHosts / -i）只有真跑才暴露."""
    import subprocess as sp

    import paramiko

    from wb import ssh

    assert ssh.cli_available(), "本机得有 ssh 客户端"
    key = paramiko.RSAKey.generate(2048)
    d = tempfile.mkdtemp(prefix="wb-key-")
    keyfile = os.path.join(d, "id_wb_test")
    key.write_private_key_file(keyfile)
    # Windows OpenSSH 会拒绝「权限太开放」的私钥（直接忽略，只报 Permission denied），
    # 测试夹具必须先把 ACL 收紧，否则测的不是连通性而是权限检查。
    sp.run(["icacls", keyfile, "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
           capture_output=True)

    srv = FakeSSHServer(user="tester", password="unused", pubkey=key).start()
    try:
        out, _err, code = ssh.run_via_cli(
            {"host": "127.0.0.1", "port": srv.port, "user": "tester",
             "auth": "key_path", "secret": keyfile},
            "hostname", timeout=20,
        )
    finally:
        srv.stop()

    assert code == 0, out
    assert "wb-test-host" in out


def test_cli_error_surfaces_open_key_permission():
    """系统 ssh 忽略了一把权限太开放的私钥时，不能只说 Permission denied."""
    from wb import ssh

    stderr = (
        "Warning: Permanently added '[127.0.0.1]:22' (RSA) to the list of known hosts.\r\n"
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\r\n"
        "@         WARNING: UNPROTECTED PRIVATE KEY FILE!          @\r\n"
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\r\n"
        "Load key \"C:\\\\tmp\\\\id\": bad permissions\r\n"
        "tester@127.0.0.1: Permission denied (publickey).\r\n"
    )
    msg = ssh._cli_error(stderr, 255)
    assert "权限" in msg and "icacls" in msg, msg
    assert "Warning: Permanently added" not in msg, "别把 known_hosts 的提示当结论"


def test_cli_error_picks_last_meaningful_line():
    from wb import ssh

    msg = ssh._cli_error("Warning: Permanently added 'x' (RSA) to the list of known hosts.\n"
                         "ssh: connect to host 1.2.3.4 port 22: Connection refused\n", 255)
    assert "Connection refused" in msg, msg


def test_import_updates_credentials_when_verified():
    """已存在但凭据猜错了（113 存的是 fcbox，实际要 deploy_key）.

    新凭据验证过就必须能覆盖，否则错误密钥永远修不好。
    """
    _setup()
    with db.tx() as c:
        c.execute(
            "INSERT INTO server(name,host,port,user,auth,secret,created_at,updated_at) "
            "VALUES('旧','203.0.113.11',22,'root','key_path','/home/dev/.ssh/old_wrong_key',?,?)",
            (db.now(), db.now()),
        )
    res = bootstrap.import_items([{
        "name": "新", "host": "203.0.113.11", "port": 22, "user": "root",
        "auth": "key_path", "secret": "/home/dev/.ssh/deploy_key", "verified": True,
    }])
    assert res["updated"] == 1 and res["added"] == 0
    assert db.scalar("SELECT secret FROM server WHERE host='203.0.113.11'") == "/home/dev/.ssh/deploy_key"


def test_import_does_not_overwrite_with_unverified():
    """没验证过的凭据不能覆盖用户现有配置 —— 那样会把人改坏的密钥推上去."""
    _setup()
    with db.tx() as c:
        c.execute(
            "INSERT INTO server(name,host,port,user,auth,secret,created_at,updated_at) "
            "VALUES('好的','1.1.1.1',22,'root','key_path','C:/good_key',?,?)",
            (db.now(), db.now()),
        )
    res = bootstrap.import_items([{
        "name": "猜的", "host": "1.1.1.1", "port": 22, "user": "root",
        "auth": "key_path", "secret": "C:/guess", "verified": False,
    }])
    assert res["skipped"] == 1 and res["updated"] == 0
    assert db.scalar("SELECT secret FROM server WHERE host='1.1.1.1'") == "C:/good_key"


# ---------------------------------------------------------------- known_hosts 端口
def test_known_hosts_keeps_nonstandard_port():
    """`[ssh.node-b.test]:2222` —— 端口就在行里，之前被丢了，全按 22 去连."""
    d = tempfile.mkdtemp(prefix="wb-kh-")
    with open(os.path.join(d, "known_hosts"), "w", encoding="utf-8") as f:
        f.write("[ssh.node-b.test]:2222 ssh-ed25519 AAAA\n")
        f.write("203.0.113.10 ssh-ed25519 AAAA\n")
        f.write("[node-a.test]:65032 ssh-ed25519 AAAA\n")
        f.write("[multi.host.cn]:22022,[other.host.cn]:2222 ssh-ed25519 AAAA\n")

    with patch.object(bootstrap.discover, "SSH_DIR", d):
        got = bootstrap.discover.ssh_candidates()["known_hosts"]

    port = {it["host"]: it["port"] for it in got}
    assert port["ssh.node-b.test"] == 2222, port
    assert port["203.0.113.10"] == 22, port
    assert port["node-a.test"] == 65032, port
    assert port["multi.host.cn"] == 22022 and port["other.host.cn"] == 2222, port


def test_kh_items_accepts_old_string_format():
    """老调用/老测试传的是纯字符串，别因此炸."""
    got = bootstrap._kh_items(["10.0.0.5", {"host": "h2", "port": 2222}])
    assert got == [{"host": "10.0.0.5", "port": 22}, {"host": "h2", "port": 2222}]


def test_find_ssh_port_returns_base_when_it_works():
    srv = FakeSSHServer(user="u", password="p").start()
    try:
        assert bootstrap.find_ssh_port("127.0.0.1", srv.port) == srv.port
    finally:
        srv.stop()


def test_find_ssh_port_probes_hints_when_base_closed():
    """22 不通时探常见端口 —— 很多人把 sshd 改到 2222."""
    srv = FakeSSHServer(user="u", password="p").start()
    with patch.object(bootstrap, "SSH_PORT_HINTS", (srv.port,)):
        try:
            assert bootstrap.find_ssh_port("127.0.0.1", 22) == srv.port
        finally:
            srv.stop()
