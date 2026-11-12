"""本机发现：目录、SSH 配置、项目里提到的服务器.

为什么要有这个模块
------------------
新用户第一次打开，最烦的是"我服务器地址是啥来着、密钥在哪"。
这些信息本机其实都有，让人重打一遍既慢又容易打错。

安全边界（重要，别越线）
------------------------
1. **只读元数据，不读私钥内容**。
   `~/.ssh/config` 里的 HostName / User / Port / IdentityFile 是普通文本；
   `id_*` 只取文件名（存在与否、类型），内容一个字节都不读 —— 内容交给 paramiko
   在真正连接时才读，不经过这里，也不进数据库。
2. **known_hosts 只取主机名**，不取公钥指纹，不判断可信。
3. **项目里扫常见的说明文档和配置文件**（README / deploy.sh / .env / docker-compose / config.*）。
   本机自用，服务器地址散在这些地方很正常；只提取"像主机的东西"，不整段复制文件内容。
4. **扫到的一切都只是"候选"**：默认不勾选、不自动保存、不自动连接。
   必须用户点「测试连接」通过后才入库。
5. 这些接口只在 127.0.0.1 上可达（app.py 只绑回环地址），别人连不进来。
"""

from __future__ import annotations

import os
import re
import string

SSH_DIR = os.path.join(os.path.expanduser("~"), ".ssh")

# 范围「无限制」：配置文件也扫（默认，自己电脑自己用）
FULL_TEXT_NAMES = (
    "README.md", "readme.md", "README.txt", "DEPLOY.md", "deploy.md",
    "deploy.sh", "Makefile", "docker-compose.yml", "docker-compose.yaml",
    "docker-compose.yml.example", ".env", ".env.example", ".env.local",
    "config.json", "config.yaml", "config.yml", "settings.py", "config.py",
)
# 范围「保守」：只看说明文档和部署脚本，不碰 .env / config / json
SAFE_TEXT_NAMES = (
    "README.md", "readme.md", "README.txt", "DEPLOY.md", "deploy.md",
    "deploy.sh", "Makefile",
)
SKIP_FILE_HINTS = ("id_rsa", "id_ed25519", "id_ecdsa")  # 私钥文件不当文档读

SCOPES = {"full": "无限制（推荐）", "safe": "保守"}

# 这些不是"要巡检的服务器"：代码托管、公共站点、路由器网关
# 三处（config / known_hosts / 项目文档）共用同一份，避免只有一处过滤造成漏网
NOT_A_SERVER = {
    "github.com", "gitlab.com", "bitbucket.org", "gitee.com", "ssh.dev.azure.com",
    "qq.com", "baidu.com", "google.com", "aliyun.com", "example.com",
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
}


def is_not_a_server(host: str) -> bool:
    """按整串和后缀判断 —— mail.qq.com 也算公共站点，不是自己的机器."""
    h = (host or "").strip().lower()
    if not h:
        return True
    if h in NOT_A_SERVER:
        return True
    return any(h.endswith("." + d) for d in NOT_A_SERVER if "." in d)

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SSH_TARGET_RE = re.compile(r"\bssh\s+(?:-\S+\s+)*(?:[\w.-]+@)?([\w.-]+\.[a-zA-Z]{2,})")
USER_HOST_RE = re.compile(r"\b([a-z_][\w.-]*)@([\w.-]+\.[a-zA-Z]{2,})")
MAX_CANDIDATES = 40


# ------------------------------------------------------------------ 目录
def drives() -> list[str]:
    """Windows 盘符列表；其它平台返回 /."""
    if os.name != "nt":
        return ["/"]
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            out.append(root)
    return out


# 系统 / 程序专属目录：不是"装项目的地方"，列出来只会让人翻半天
SYSTEM_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "system volume information", "recovery", "perflogs", "$recycle.bin",
    "config.msi", "documents and settings", "inetpub", "$winreagent",
    "onedrivetemp", "msocache", "intel", "amd", "nvidia", "drivers",
    "esd", "$windows.~bt", "$windows.~ws", "temp", "tmp",
}


def list_dirs(path: str) -> list[dict]:
    """列子目录。系统目录不隐藏（诚实），但沉到最后并标出来，别挡着常用目录."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return []
    try:
        names = sorted(os.listdir(path), key=lambda s: s.lower())
    except OSError:
        return []

    normal: list[dict] = []
    system: list[dict] = []
    for name in names:
        if name.startswith(".") or name.startswith("$"):
            continue
        full = os.path.join(path, name)
        try:
            if not os.path.isdir(full):
                continue
        except OSError:
            continue
        item = {"name": name, "path": full, "system": name.lower() in SYSTEM_DIRS}
        (system if item["system"] else normal).append(item)

    return (normal + system)[:300]


def parent_of(path: str) -> str:
    path = os.path.abspath(path)
    up = os.path.dirname(path)
    return up if up and up != path else path


# ------------------------------------------------------------------ SSH
def _ssh_config_blocks(text: str) -> list[dict]:
    """极简 ssh_config 解析：只认 Host / HostName / User / Port / IdentityFile."""
    hosts: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, val = parts[0].lower(), parts[1].strip()
        if key == "host":
            if cur:
                hosts.append(cur)
            alias = val.split()[0]
            cur = {} if alias == "*" else {"alias": alias}
            continue
        if cur is None:
            continue
        if key == "hostname":
            cur["host"] = val
        elif key == "user":
            cur["user"] = val
        elif key == "port":
            cur["port"] = val
        elif key == "identityfile":
            # 一个 Host 可以写多个 IdentityFile（主密钥 + 兜底），全都留着逐个试。
            # 之前只取最后一个，导致 101 的新主密钥被旧兜底密钥覆盖，怎么都连不上。
            path = os.path.expanduser(val.strip('"'))
            cur.setdefault("identity_files", [])
            if path not in cur["identity_files"]:
                cur["identity_files"].append(path)
    if cur:
        hosts.append(cur)
    return [h for h in hosts if h.get("alias") or h.get("host")]


def ssh_candidates() -> dict:
    """本机 SSH 线索：config 里的主机、可用密钥文件名、known_hosts 里的主机名."""
    result: dict = {"config": [], "keys": [], "known_hosts": [], "ssh_dir": SSH_DIR}
    if not os.path.isdir(SSH_DIR):
        return result

    cfg_path = os.path.join(SSH_DIR, "config")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8", errors="ignore") as f:
                text = f.read(256 * 1024)
        except OSError:
            text = ""
        for item in _ssh_config_blocks(text)[:MAX_CANDIDATES]:
            if is_not_a_server(item.get("host") or item.get("alias") or ""):
                continue
            files = [f for f in (item.get("identity_files") or []) if os.path.isfile(f)]
            # 最新改过的排前面：换机后新加的密钥通常比兜底旧密钥新，
            # 先试对的能少一次认证失败（服务器 MaxAuthTries 有限）
            files.sort(key=lambda f: -os.path.getmtime(f))
            result["config"].append(
                {
                    "alias": item.get("alias") or item.get("host") or "",
                    "host": item.get("host") or item.get("alias") or "",
                    "user": item.get("user") or "root",
                    "port": item.get("port") or "22",
                    "key": files[0] if files else "",
                    "keys": files,
                }
            )

    # 只取文件名与类型，不读内容
    try:
        for name in sorted(os.listdir(SSH_DIR)):
            if not name.startswith("id_") and not name.endswith(".pem"):
                continue
            full = os.path.join(SSH_DIR, name)
            if not os.path.isfile(full):
                continue
            kind = "私钥"
            if name.endswith(".pub"):
                kind = "公钥"
                continue
            result["keys"].append({"path": full, "name": name, "kind": kind})
    except OSError:
        pass

    kh_path = os.path.join(SSH_DIR, "known_hosts")
    if os.path.isfile(kh_path):
        seen: list[str] = []
        items: list[dict] = []
        try:
            with open(kh_path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("|"):
                        continue  # |1|... 是加盐哈希，解不出主机名
                    parts = line.split()
                    if not parts:
                        continue
                    token = parts[1] if parts[0].startswith("@") and len(parts) > 1 else parts[0]
                    for one in token.split(","):
                        host, port = _split_host_port(one)
                        if host and host not in seen and not is_not_a_server(host):
                            seen.append(host)
                            items.append({"host": host, "port": port})
        except OSError:
            pass
        result["known_hosts"] = items[:MAX_CANDIDATES]

    return result


def _split_host_port(token: str) -> tuple[str, int]:
    """`[ssh.node-b.test]:2222` → (ssh.node-b.test, 2222)。

    非标端口的机器在 known_hosts 里就是这么记的 —— 之前只取主机名，
    端口信息白白丢掉，于是所有 2222 / 65032 的机器都被当成 22 去连。
    """
    t = (token or "").strip()
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        port = 22
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
            except ValueError:
                port = 22
        return host, port
    return t, 22


# ------------------------------------------------- 项目里提到的服务器（只读文档）
def _read_small(path: str, limit: int = 64 * 1024) -> str:
    try:
        size = os.path.getsize(path)
        if size > limit * 4:
            return ""
        with open(path, "rb") as f:
            raw = f.read(limit)
    except OSError:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def project_hosts(roots: list[str], max_projects: int = 60, scope: str = "full") -> list[dict]:
    """从项目的说明文档 / 部署脚本里找 IP 和 ssh 目标.

    只当"线索"用：标出来源文件，默认不勾选。绝不扫 .env / yml / json。
    """
    found: dict[str, dict] = {}
    checked = 0
    names = FULL_TEXT_NAMES if scope == "full" else SAFE_TEXT_NAMES

    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            if checked >= max_projects:
                break
            proj = os.path.join(root, name)
            if not os.path.isdir(proj):
                continue
            checked += 1
            for fname in names:
                fpath = os.path.join(proj, fname)
                if not os.path.isfile(fpath):
                    continue
                if any(hint in fname.lower() for hint in SKIP_FILE_HINTS):
                    continue
                text = _read_small(fpath)
                if not text:
                    continue

                for ip in IPV4_RE.findall(text):
                    if ip.startswith(("0.", "127.", "255.")) or is_not_a_server(ip):
                        continue
                    found.setdefault(ip, {"host": ip, "user": "", "port": "22",
                                          "from": f"{name}/{fname}"})
                for m in USER_HOST_RE.finditer(text):
                    user, host = m.group(1), m.group(2)
                    if host.endswith((".md", ".sh", ".yml", ".json")):
                        continue
                    if is_not_a_server(host):
                        continue
                    found.setdefault(host, {"host": host, "user": user, "port": "22",
                                            "from": f"{name}/{fname}"})
                for m in SSH_TARGET_RE.finditer(text):
                    host = m.group(1)
                    if is_not_a_server(host):
                        continue
                    found.setdefault(host, {"host": host, "user": "", "port": "22",
                                            "from": f"{name}/{fname}"})

    return list(found.values())[:MAX_CANDIDATES]
