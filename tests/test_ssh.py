"""T8 SSH 连接层与巡检解析验收.

连接测试跑本地假 SSH 服务端（tests/_sshd.py），不依赖真实机器、不需要网络。
"""

from __future__ import annotations

import os
import socket
import tempfile
import time

import paramiko

from _sshd import FAKE_STDOUT, FakeSSHServer, SilentTCPServer
from wb import probe, ssh


def _srv(password: str = "s3cret", pubkey=None) -> FakeSSHServer:
    return FakeSSHServer(user="tester", password=password, pubkey=pubkey).start()


def _cfg(port: int, auth: str = "password", secret: str = "s3cret") -> dict:
    return {"host": "127.0.0.1", "port": port, "user": "tester", "auth": auth, "secret": secret}


# ------------------------------------------------------------ 认证
def test_password_auth_connects_and_runs():
    s = _srv()
    try:
        c = ssh.open_session(_cfg(s.port))
        out, err, code = ssh.run(c, "hostname")
        ssh.close(c)
        assert code == 0, f"exit={code} err={err}"
        assert out.strip() == "wb-test-host"
    finally:
        s.stop()


def test_key_path_auth_connects():
    key = paramiko.RSAKey.generate(2048)
    d = tempfile.mkdtemp(prefix="wb-key-")
    key_file = os.path.join(d, "id_rsa")
    key.write_private_key_file(key_file)

    s = _srv(pubkey=key)
    try:
        c = ssh.open_session(_cfg(s.port, auth="key_path", secret=key_file))
        out, _, code = ssh.run(c, "hostname")
        ssh.close(c)
        assert code == 0 and "wb-test-host" in out
    finally:
        s.stop()


def test_wrong_password_returns_readable_chinese():
    s = _srv()
    try:
        try:
            ssh.open_session(_cfg(s.port, secret="wrong"))
        except ssh.SSHError as err:
            msg = str(err)
            assert "认证失败" in msg, msg
            assert "Traceback" not in msg
            assert "127.0.0.1" in msg, "错误信息里要带主机，不然用户不知道是哪台"
        else:
            raise AssertionError("错误密码竟然连上了")
    finally:
        s.stop()


def test_connection_refused_is_readable():
    # 找一个没人监听的端口
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    try:
        ssh.open_session(_cfg(port))
    except ssh.SSHError as err:
        msg = str(err)
        assert "拒绝" in msg or "不通" in msg, msg
        assert "Traceback" not in msg
    else:
        raise AssertionError("连不上的端口竟然成功了")


def test_timeout_returns_within_limit():
    """端口通但 SSH 不说话 → 必须按 TIMEOUT 返回，不能一直挂."""
    silent = SilentTCPServer()
    old = ssh.TIMEOUT
    ssh.TIMEOUT = 2
    try:
        t0 = time.time()
        try:
            ssh.open_session(_cfg(silent.port))
        except ssh.SSHError as err:
            msg = str(err)
            assert "超时" in msg or "没有回应" in msg, msg
            assert "Traceback" not in msg
        else:
            raise AssertionError("无响应的端口竟然连上了")
        cost = time.time() - t0
        assert cost < 8, f"超时保护没生效，卡了 {cost:.1f}s"
    finally:
        ssh.TIMEOUT = old
        silent.stop()


def test_test_connection_helper():
    s = _srv()
    try:
        ok, detail = ssh.test_connection(_cfg(s.port))
        assert ok is True and detail == "wb-test-host"
        ok2, detail2 = ssh.test_connection(_cfg(s.port, secret="nope"))
        assert ok2 is False and "认证失败" in detail2
    finally:
        s.stop()


# ------------------------------------------------------------ probe 解析
def test_split_sections():
    sec = probe.split_sections(FAKE_STDOUT)
    assert sec["HOST"] == "wb-test-host"
    assert "PRETTY_NAME" in sec["OS"]
    assert sec["DOCKER"].splitlines() == ["demo-collector", "mysql"]


def test_parse_os():
    assert probe.parse_os('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\n') == "Ubuntu 24.04.1 LTS"
    assert probe.parse_os("") == ""


def test_parse_mem_uses_available():
    used, total, pct = probe.parse_mem(
        "              total        used        free      shared  buff/cache   available\n"
        "Mem:           7943        2311         512          12        5119        5377\n"
    )
    assert total == 7943
    assert used == 7943 - 5377
    assert pct == round((7943 - 5377) * 100 / 7943, 1)


def test_parse_mem_without_available_column():
    used, total, pct = probe.parse_mem("Mem:  1000  400  600\n")
    assert (used, total) == (400, 1000)
    assert pct == 40.0


def test_parse_disk():
    used, total, pct = probe.parse_disk(
        "Filesystem      Size  Used Avail Use% Mounted on\n/dev/vda1        59G   12G   45G  21% /\n"
    )
    assert (used, total, pct) == ("12G", "59G", 21.0)


def test_parse_ports_dedupes():
    ports = probe.parse_ports(
        "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
        "LISTEN 0 128 0.0.0.0:80 0.0.0.0:*\n"
        "LISTEN 0 128 [::]:443 [::]:*\n"
        "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
    )
    assert ports == [":22", ":80", ":443"]


def test_parse_uptime():
    assert probe.parse_uptime("94500.12 123.4") == "1 天 2 小时"
    assert probe.parse_uptime("3600.0 0") == "1 小时"
    assert probe.parse_uptime("garbage") == ""


def test_parse_load():
    assert probe.parse_load("0.42 0.31 0.28 1/234 5678") == 0.42
    assert probe.parse_load("") is None


def test_parse_full_snapshot():
    snap = probe.parse(FAKE_STDOUT)
    assert snap.hostname == "wb-test-host"
    assert snap.os_name.startswith("Ubuntu 24.04")
    assert snap.kernel == "6.8.0-45-generic"
    assert snap.load1 == 0.42
    assert snap.disk_pct == 21.0
    assert snap.containers == ["demo-collector", "mysql"]
    assert ":22" in snap.ports and ":443" in snap.ports


def test_probe_command_has_no_user_input():
    """命令串必须是常量：出现格式化占位就说明有人塞了变量."""
    assert "{" not in probe.PROBE_CMD.replace("{{.Names}}", "")
    assert "%s" not in probe.PROBE_CMD


# ---------------------------------------------------------------- 免认证（用户名即凭据）
def test_auth_none_connects_via_paramiko():
    """某些云沙箱 这类沙箱：token 当用户名，无密钥无密码.

    paramiko 的 SSHClient.connect() 里没有 auth_none 分支，只能自己建 transport。
    """
    srv = FakeSSHServer(user="ibssh_token", password="", pubkey=None, allow_none=True).start()
    try:
        client = ssh.open_session(
            {"host": "127.0.0.1", "port": srv.port, "user": "ibssh_token",
             "auth": "none", "secret": ""},
            timeout=10,
        )
        try:
            out, _err, code = ssh.run(client, "hostname", timeout=10)
        finally:
            ssh.close(client)
    finally:
        srv.stop()

    assert code == 0, out
    assert "wb-test-host" in out


def test_auth_none_rejected_is_readable():
    """服务器不放行时，报错要能看懂，别吐 paramiko 的原始异常名."""
    srv = FakeSSHServer(user="someone_else", password="", allow_none=True).start()
    try:
        try:
            ssh.open_session(
                {"host": "127.0.0.1", "port": srv.port, "user": "wrong_user",
                 "auth": "none", "secret": ""},
                timeout=10,
            )
        except ssh.SSHError as err:
            assert "认证" in str(err) or "拒" in str(err), str(err)
        else:
            raise AssertionError("用户名不对不该连上")
    finally:
        srv.stop()


# ---------------------------------------------------------------- 端口明细
def test_parse_port_details_reads_ss_output():
    """`ss -tlnpH` 要能解析出「谁在听、绑哪个地址」."""
    raw = (
        'LISTEN 0 4096 127.0.0.54:53 0.0.0.0:* users:(("systemd-resolve",pid=558,fd=17))\n'
        'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=890,fd=3))\n'
    )
    got = probe.parse_port_details(raw)
    assert len(got) == 2
    by_port = {d["port"]: d for d in got}
    assert by_port[22]["proc"] == "sshd"
    assert by_port[22]["scope"] == "all"
    assert by_port[53]["scope"] == "local", "127.x 是仅本机"


def test_parse_port_details_without_process_column():
    """没有 root 时 ss 不给 users 段，仍要能出端口和地址，只是进程为空."""
    raw = "LISTEN 0 128 0.0.0.0:80 0.0.0.0:*\n"
    got = probe.parse_port_details(raw)
    assert got == [{"port": 80, "addr": "0.0.0.0", "proc": "", "scope": "all"}]


def test_parse_port_details_reads_netstat_output():
    raw = "tcp 0 0 0.0.0.0:22 0.0.0.0:* LISTEN 890/sshd\n"
    got = probe.parse_port_details(raw)
    assert got and got[0]["port"] == 22 and got[0]["proc"] == "sshd"


def test_parse_port_details_handles_empty():
    assert probe.parse_port_details("") == []
    assert probe.parse_port_details("State Recv-Q Send-Q\n") == []
