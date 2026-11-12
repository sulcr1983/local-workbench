"""SSH 连接（paramiko 单轨）.

纪律：
- 所有失败都转成人能看懂的中文，控制台不许吐 traceback（T8 验收）。
- 超时 18s，连接/认证/banner 三段都限住，避免卡死。
- 老服务器只支持旧 kex 时，给出明确原因，并提供 legacy_kex 开关。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import socket

import paramiko

TIMEOUT = 18

# paramiko 的 Transport 线程会用 logger.exception 把异常打到 stderr，
# 界面上就会冒出 traceback（T8 明确不允许）。这里把它接住：
# 有 workbench.log 就写文件，没有就静默 —— 但调用方仍会拿到中文错误。
_plog = logging.getLogger("paramiko.transport")
if not _plog.handlers:
    _plog.addHandler(logging.NullHandler())
_plog.propagate = False

# paramiko 3.x 默认仍带 group14-sha1，但 group1-sha1 这类更老的默认不给。
# 打开 legacy_kex 时把它们插到首选列表最前面（私有属性，仅本机工具可接受）。
LEGACY_KEX = (
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group1-sha1",
    "diffie-hellman-group-exchange-sha1",
)
_legacy_applied = False


def _enable_legacy_kex() -> None:
    global _legacy_applied
    if _legacy_applied:
        return
    current = tuple(paramiko.Transport._preferred_kex)  # type: ignore[attr-defined]
    extra = tuple(k for k in LEGACY_KEX if k not in current)
    paramiko.Transport._preferred_kex = extra + current  # type: ignore[attr-defined]
    _legacy_applied = True


class SSHError(Exception):
    """带中文说明的连接失败；message 可以直接展示给用户."""


def _humanize(err: Exception, host: str, port: int) -> str:
    text = str(err)
    low = text.lower()

    if isinstance(err, paramiko.AuthenticationException):
        return f"认证失败：{host} 拒绝了这组用户名/密码（或私钥）。检查一下凭据，或者这台机器禁用了密码登录。"
    if isinstance(err, paramiko.BadHostKeyException):
        return f"主机指纹变了：{host} 返回的公钥和本地记录不一致。如果是你重装过系统，删掉 known_hosts 里这一条再连。"
    if isinstance(err, socket.timeout) or "timed out" in low:
        return f"连接超时（{TIMEOUT}s）：{host}:{port} 没有响应。可能是防火墙挡了、IP 变了，或者机器关机了。"
    if "banner" in low or "no existing session" in low:
        # banner 超时后 paramiko 内部会重试一次，最终以 "No existing session" 抛出
        return (
            f"连上了 {host}:{port}，但对方没有回应 SSH 握手（等了 {TIMEOUT}s）。\n"
            f"通常是端口填错了（不是 SSH 端口），或者中间有设备在拦。"
        )
    if isinstance(err, socket.gaierror) or "name or service not known" in low or "getaddrinfo" in low:
        return f"域名解析失败：找不到 {host}。检查一下主机名有没有写错。"
    if "no acceptable kex algorithm" in low:
        return (
            f"{host} 提出的密钥交换算法和本机没有交集 —— 握手谈不拢。\n"
            f"常见原因：中间有设备改过 SSH 握手（防火墙、跳板、蜜罐），或者服务端配置特殊。"
        )
    if "no acceptable" in low and "cipher" in low:
        return f"加密算法不匹配：{host} 只支持当前策略不接受的 cipher。可以试着打开「兼容老服务器」。"
    if "connection refused" in low:
        return f"连接被拒绝：{host}:{port} 上没有 SSH 服务在监听（端口写错了？服务没启动？）。"
    if "unable to connect" in low or isinstance(err, OSError):
        return f"网络不通：连不上 {host}:{port}（{text or '无更多信息'}）。"
    if isinstance(err, paramiko.SSHException):
        return f"SSH 协议错误：{text}"
    return f"连接失败：{text or type(err).__name__}"


def _auth_kwargs(server: dict) -> dict:
    """把库里的认证字段翻译成 paramiko 参数."""
    auth = (server.get("auth") or "key_path").lower()
    secret = server.get("secret") or ""
    passphrase = server.get("passphrase") or "" or None

    if auth == "password":
        return {"password": secret, "allow_agent": False, "look_for_keys": False}
    if auth == "key_path":
        # secret 里可能有多把私钥（换行或分号分隔）—— 依次试，第一把能过就行
        paths = [p.strip() for p in secret.replace(";", "\n").splitlines() if p.strip()]
        paths = [p for p in paths if os.path.isfile(p)] or paths
        target = paths if len(paths) > 1 else (paths[0] if paths else secret)
        return {
            "key_filename": target,
            "passphrase": passphrase,
            "allow_agent": False,
            "look_for_keys": False,
        }
    if auth == "key_content":
        import io

        try:
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(secret), password=passphrase)
        except paramiko.SSHException:
            try:
                pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(secret), password=passphrase)
            except paramiko.SSHException as err:
                raise SSHError(f"私钥内容解析失败：{err}。确认粘贴的是完整的私钥（含 BEGIN/END 行）。") from err
        return {"pkey": pkey, "allow_agent": False, "look_for_keys": False}
    if auth == "agent":
        return {"allow_agent": True, "look_for_keys": True}
    if auth == "none":
        # 用户名本身就是凭据（某些云沙箱 这类沙箱：token 当用户名，无密钥无密码）。
        # paramiko 允许一个认证方式都不给，服务器愿意就直接放行。
        return {"allow_agent": False, "look_for_keys": False}
    raise SSHError(f"不支持的认证方式：{auth}")


def _client(legacy: bool) -> paramiko.SSHClient:
    if legacy:
        _enable_legacy_kex()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return c


def _connect_none(client: paramiko.SSHClient, host: str, port: int, user: str,
                  t: int, sock=None) -> None:
    """免认证登录（用户名即凭据）.

    paramiko 的 `SSHClient._auth()` 里没有 auth_none 分支 —— 它只会试密钥 / agent /
    密码，所以「服务器只认 none」时永远过不去。这里绕开 connect()，自己建 transport
    再调 `auth_none()`。某些云沙箱 这类沙箱就是这么设计的（token 当用户名）。
    """
    if sock is None:
        sock = socket.create_connection((host, port), timeout=t)
    tr = paramiko.Transport(sock)
    tr.banner_timeout = t
    tr.auth_timeout = t
    try:
        tr.start_client(timeout=t)
        tr.auth_none(user)
    except Exception:
        tr.close()
        raise
    client._transport = tr  # type: ignore[attr-defined]  # open_session 靠它拿通道


def open_session(server: dict, jump: str = "", timeout: int | None = None) -> paramiko.SSHClient:
    """建立到目标机的 SSH 会话；失败抛 SSHError（中文，可直接显示）.

    timeout 显式给值时用它的 —— 一键发现试连要快，不希望每台卡 18 秒。
    """
    t = timeout or TIMEOUT
    host = (server.get("host") or "").strip()
    port = int(server.get("port") or 22)
    user = (server.get("user") or "root").strip()
    legacy = bool(server.get("legacy_kex"))
    if not host:
        raise SSHError("没填主机地址")

    kwargs = _auth_kwargs(server)
    client = _client(legacy)
    no_auth = (server.get("auth") or "").lower() == "none"

    jump = (jump or server.get("jump") or "").strip()
    try:
        if jump and jump != "none":
            jump_host, jump_port, jump_user = _parse_jump(jump, user)
            jc = _client(legacy)
            jc.connect(
                jump_host,
                port=jump_port,
                username=jump_user,
                timeout=t,
                banner_timeout=t,
                auth_timeout=t,
                **kwargs,
            )
            channel = jc.get_transport().open_channel(
                "direct-tcpip", (host, port), ("127.0.0.1", 0)
            )
            client.connect(
                host,
                port=port,
                username=user,
                sock=channel,
                timeout=t,
                banner_timeout=t,
                auth_timeout=t,
                **kwargs,
            )
            # 让目标会话持有跳板连接，避免被 GC
            client._wb_jump = jc  # type: ignore[attr-defined]
        elif no_auth:
            _connect_none(client, host, port, user, t)
        else:
            client.connect(
                host,
                port=port,
                username=user,
                timeout=t,
                banner_timeout=t,
                auth_timeout=t,
                **kwargs,
            )
    except SSHError:
        client.close()
        raise
    except Exception as err:  # noqa: BLE001 - 统一转中文，绝不让栈冒到界面
        client.close()
        raise SSHError(_humanize(err, host, port)) from err

    return client


def _parse_jump(jump: str, default_user: str) -> tuple[str, int, str]:
    """支持 "user@host:port" 和纯 "host"."""
    user = default_user
    rest = jump
    if "@" in rest:
        user, rest = rest.split("@", 1)
    port = 22
    if ":" in rest:
        rest, port_s = rest.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            port = 22
    return rest, port, user


def run(client: paramiko.SSHClient, command: str, timeout: int = TIMEOUT) -> tuple[str, str, int]:
    """执行只读命令；返回 (stdout, stderr, exit_code)，绝不抛异常."""
    try:
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")
        code = stdout.channel.recv_exit_status()
        return out, err, code
    except Exception as exc:  # noqa: BLE001
        return "", _humanize(exc, "远端", 22), 255


def close(client: paramiko.SSHClient | None) -> None:
    if client is None:
        return
    jump = getattr(client, "_wb_jump", None)
    try:
        client.close()
    finally:
        if jump is not None:
            try:
                jump.close()
            except Exception:  # noqa: BLE001
                pass


def test_connection(server: dict) -> tuple[bool, str]:
    """验证连接：成功返回 (True, hostname)，失败 (False, 中文原因)."""
    client = None
    try:
        client = open_session(server)
        out, err, code = run(client, "hostname")
        if code != 0:
            return False, err.strip() or f"命令返回码 {code}"
        return True, out.strip() or "(没有返回主机名)"
    except SSHError as err:
        return False, str(err)
    finally:
        close(client)

# ------------------------------------------------------------------ 系统 ssh 后备
# paramiko 至今不支持 sntrup761x25519（OpenSSH 9.x 的后量子 kex），
# 而不少新装服务器只开这个算法。本机装了 OpenSSH 客户端就用它兜底。
_CLI_CANDIDATES = (
    r"C://Windows//System32//OpenSSH//ssh.exe",
    "/usr/bin/ssh",
    "/usr/local/bin/ssh",
    "/opt/homebrew/bin/ssh",
)


def cli_available() -> str:
    """返回可用的 ssh 客户端路径；没有就空串."""
    for path in _CLI_CANDIDATES:
        if os.path.isfile(path):
            return path
    return shutil.which("ssh") or ""


def _win_short_path(path: str) -> str:
    """含空格的路径塞进 `-o key=value` 会被 ssh 的空格分词截断，转 8.3 短路径.

    OpenSSH 解析 -o 的值时按空白切分，`UserKnownHostsFile=C:\\Program Files\\x`
    会变成 `C:\\Program`，然后 ssh 抱怨路径不存在（还不报错，只是不记录指纹）。
    """
    if os.name != "nt" or " " not in path:
        return path
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
        return buf.value if n else path
    except Exception:  # noqa: BLE001 - 转不了就原样用，别影响连接
        return path


def _cli_known_hosts() -> str:
    """系统 ssh 用的 known_hosts —— 用应用自己的一份，不碰用户 ~/.ssh/known_hosts.

    每次探测都会给新机器记指纹，写进用户的 known_hosts 属于污染别人的文件。
    """
    from . import config

    path = config.KNOWN_HOSTS
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
    except OSError:
        return "NUL"
    return path


def _askpass_env(password: str) -> tuple[dict, str]:
    """造一个「一次性 askpass」环境：让系统 ssh 能拿到密码，不靠 TTY.

    Windows 的 OpenSSH 用 CreateProcess 启动 SSH_ASKPASS 指定的程序，
    所以只认 .exe/.bat（.sh 报 CreateProcessW error:193）。
    .bat 内容固定为纯 ASCII 的 `type "%WB_ASKPASS_FILE%"`：
    密码走文件，避免 cmd 解析 `& < > | ^ %` 这些字符（实测特殊字符与中文都能原样送达）。

    返回 (env, 临时目录)；临时目录用完必须整个删掉。
    """
    import tempfile

    d = tempfile.mkdtemp(prefix="wb-ask-")
    pw = os.path.join(d, "p.pw")
    with open(pw, "wb") as f:
        f.write(password.encode("utf-8"))
    bat = os.path.join(d, "askpass.bat")
    with open(bat, "wb") as f:
        f.write(b'@echo off\r\ntype "%WB_ASKPASS_FILE%"\r\n')

    env = dict(os.environ)
    env.update(
        {
            "SSH_ASKPASS": bat,
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": ":0",          # 老版本按「有 DISPLAY 才用 askpass」判断
            "WB_ASKPASS_FILE": pw,
        }
    )
    return env, d


def _cli_error(err_text: str, code: int) -> str:
    """从 ssh 的 stderr 里挑真正有用的那行 —— 别把 known_hosts 的 Warning 当结论.

    系统 ssh 在权限不对时会**直接忽略**私钥，最后只吐一句 Permission denied，
    看着像密钥错、其实是被自己的权限检查挡了。这种情况必须点明。
    """
    lines = [ln.strip() for ln in err_text.splitlines() if ln.strip()]
    useful = [ln for ln in lines if not ln.startswith("Warning: Permanently added")]
    body = "\n".join(useful)

    if "UNPROTECTED PRIVATE KEY FILE" in body or "bad permissions" in body:
        return (
            "私钥文件权限太开放，系统 ssh 直接忽略了这把钥匙（不是密钥本身不对）。"
            '收紧一下：icacls "<私钥路径>" /inheritance:r /grant:r "%USERNAME%:F"'
        )
    if not useful:
        return f"系统 ssh 返回码 {code}，没有任何输出"
    # 结论句都在最后一行（Permission denied / Connection refused / timed out）
    return useful[-1][:200]


def run_via_cli(server: dict, command: str, timeout: int = 25) -> tuple[str, str, int]:
    """用系统 ssh 跑一条命令（密钥 / agent / 密码三种都行）.

    算法兼容性比 paramiko 好得多 —— 服务器只开后量子 kex 时，只有它能连。
    密码认证走 SSH_ASKPASS（Windows 原生支持，不需要 sshpass / plink）。
    """
    exe = cli_available()
    if not exe:
        raise SSHError("本机没有 ssh 客户端，连不了只支持新算法的服务器")

    host = (server.get("host") or "").strip()
    port = int(server.get("port") or 22)
    user = (server.get("user") or "root").strip()
    auth = (server.get("auth") or "key_path").lower()
    secret = server.get("secret") or ""

    askdir = ""
    env = None
    args = [exe]
    if auth == "password":
        if not secret:
            raise SSHError("这台机器要密码，但没填密码")
        if os.name != "nt":
            raise SSHError("密码认证走系统 ssh 的兜底目前只做了 Windows（SSH_ASKPASS）")
        env, askdir = _askpass_env(secret)
        args += [
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
        ]
    elif auth == "none":
        # 用户名即凭据：别让客户端拿本机一堆密钥去乱试，会把 MaxAuthTries 撞掉
        args += ["-o", "PreferredAuthentications=none", "-o", "PubkeyAuthentication=no"]
    else:
        keys = [p.strip() for p in secret.replace(";", "\n").splitlines() if p.strip()]
        args += ["-o", "BatchMode=yes"]
        for k in keys:
            if os.path.isfile(k) or k.lower().endswith(".pem"):
                args += ["-i", k]

    args += [
        "-o", "StrictHostKeyChecking=accept-new",
        # 用本应用自己的 known_hosts：别往用户 ~/.ssh/known_hosts 里塞别人的指纹
        "-o", f"UserKnownHostsFile={_win_short_path(_cli_known_hosts())}",
        "-o", f"ConnectTimeout={min(timeout, 15)}",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
        "-p", str(port),
        "-l", user,
        host, command,
    ]

    try:
        proc = subprocess.run(
            args, capture_output=True, timeout=timeout, check=False,
            env=env, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as err:
        raise SSHError(f"系统 ssh 超时（{timeout}s）：{host}:{port} 没响应") from err
    except OSError as err:
        raise SSHError(f"系统 ssh 启动失败：{err}") from err
    finally:
        if askdir:
            shutil.rmtree(askdir, ignore_errors=True)

    out = proc.stdout.decode("utf-8", errors="ignore")
    err_text = proc.stderr.decode("utf-8", errors="ignore")
    if proc.returncode != 0 and not out.strip():
        raise SSHError(f"系统 ssh 连不上：{_cli_error(err_text, proc.returncode)}")
    return out, err_text, proc.returncode
