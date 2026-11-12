"""测试专用：最小 SSH 服务端.

只做三件事：接受连接、验密码/公钥、对任何 exec 回一段固定输出。
不依赖系统 sshd，也不碰真实机器 —— 测试要能离线跑。
"""

from __future__ import annotations

import socket
import threading

import paramiko

# 一段真实的 Ubuntu 巡检输出（含跑着 docker 容器和两个监听端口）
FAKE_STDOUT = """###WB-HOST
wb-test-host
###WB-OS
NAME="Ubuntu"
VERSION="24.04.1 LTS (Noble Numbat)"
PRETTY_NAME="Ubuntu 24.04.1 LTS"
###WB-KERNEL
6.8.0-45-generic
###WB-UPTIME
94500.12 1234567.89
###WB-MEM
               total        used        free      shared  buff/cache   available
Mem:            7943        2311         512          12        5119        5377
Swap:           2047           0        2047
###WB-DISK
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda1        59G   12G   45G  21% /
###WB-LOAD
0.42 0.31 0.28 1/234 5678
###WB-DOCKER
demo-collector
mysql
###WB-PORTS
LISTEN 0      128          0.0.0.0:22        0.0.0.0:*
LISTEN 0      128          0.0.0.0:80        0.0.0.0:*
LISTEN 0      128             [::]:443          [::]:*
###WB-END
"""


class _Handler(paramiko.ServerInterface):
    def __init__(self, user: str, password: str, pubkey: paramiko.PKey | None,
                 allow_none: bool = False):
        self.user = user
        self.password = password
        self.pubkey = pubkey
        self.allow_none = allow_none

    def check_auth_none(self, username):
        """免认证：只认用户名（某些云沙箱 的 token-as-username 就是这么走的）."""
        if self.allow_none and username == self.user:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        if username == self.user and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if self.pubkey is not None and username == self.user and key.get_base64() == self.pubkey.get_base64():
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password,publickey" if not self.allow_none else "none"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        # paramiko 传进来的是 bytes，别拿 str 比
        cmd = command.decode("utf-8", "ignore").strip() if isinstance(command, bytes) else str(command).strip()
        payload = "wb-test-host\n" if cmd == "hostname" else FAKE_STDOUT
        try:
            channel.sendall(payload.encode("utf-8"))
            channel.send_exit_status(0)
        finally:
            threading.Timer(0.15, channel.close).start()
        return True


class FakeSSHServer:
    def __init__(self, user: str = "tester", password: str = "s3cret", pubkey=None,
                 allow_none: bool = False):
        self.user = user
        self.password = password
        self.pubkey = pubkey
        self.allow_none = allow_none
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> "FakeSSHServer":
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        transport = None
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(self.host_key)
            transport.start_server(server=_Handler(self.user, self.password, self.pubkey, self.allow_none))
            chan = transport.accept(10)
            if chan is not None:
                while not chan.closed:
                    threading.Event().wait(0.1)
        except Exception:  # noqa: BLE001 - 测试服务端，任何异常都只影响本连接
            pass
        finally:
            if transport is not None:
                transport.close()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class SilentTCPServer:
    """只 listen 不说话：用来模拟「端口通但 SSH 无响应」的超时场景."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
                self._conns.append(c)  # 收下就不管，让客户端等到超时
            except OSError:
                return

    def stop(self) -> None:
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
