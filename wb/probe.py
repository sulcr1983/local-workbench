"""只读巡检：命令常量 + stdout 解析.

安全底线：命令串是**硬编码常量**，永远不拼接用户输入（TECH_SPEC §7.3）。
解析函数全是纯函数，用假 stdout 就能测，不依赖真机器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MARK = "###WB-"

PROBE_CMD = (
    f"echo '{MARK}HOST'; hostname 2>/dev/null; "
    f"echo '{MARK}OS'; cat /etc/os-release 2>/dev/null; "
    f"echo '{MARK}KERNEL'; uname -r 2>/dev/null; "
    f"echo '{MARK}UPTIME'; cat /proc/uptime 2>/dev/null; "
    f"echo '{MARK}MEM'; free -m 2>/dev/null; "
    f"echo '{MARK}DISK'; df -h / 2>/dev/null; "
    f"echo '{MARK}LOAD'; cat /proc/loadavg 2>/dev/null; "
    f"echo '{MARK}DOCKER'; docker ps --format '{{{{.Names}}}}' 2>/dev/null; "
    # 带 -p 才能读到「谁在监听」。没有 root 时 ss -p 仍会输出端口和地址，
    # 只是 users:(...) 段缺失 —— 所以两种输出都要能解析。
    f"echo '{MARK}PORTS'; (ss -tlnpH 2>/dev/null || ss -tlnH 2>/dev/null || netstat -tlnp 2>/dev/null || netstat -tln 2>/dev/null); "
    f"echo '{MARK}END'"
)

SECTIONS = ("HOST", "OS", "KERNEL", "UPTIME", "MEM", "DISK", "LOAD", "DOCKER", "PORTS", "END")


@dataclass
class Snapshot:
    hostname: str = ""
    os_name: str = ""
    kernel: str = ""
    uptime_text: str = ""
    load1: float | None = None
    mem_used: int | None = None       # MB
    mem_total: int | None = None      # MB
    mem_pct: float | None = None
    disk_used: str = ""
    disk_total: str = ""
    disk_pct: float | None = None
    containers: list[str] = field(default_factory=list)
    ports: list[str] = field(default_factory=list)
    port_details: list = field(default_factory=list)   # [{port,addr,proc,scope}]
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 分段
def split_sections(text: str) -> dict[str, str]:
    """按 ###WB-XXX 标记切段；缺段返回空串，不抛异常."""
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(MARK):
            if current:
                out[current] = "\n".join(buf).strip()
            name = stripped[len(MARK):].strip()
            current = name if name in SECTIONS else None
            buf = []
            continue
        if current:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    return out


# ---------------------------------------------------------------- 解析
def parse_os(text: str) -> str:
    """PRETTY_NAME="Ubuntu 24.04.1 LTS" → Ubuntu 24.04.1 LTS."""
    for line in (text or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    for line in (text or "").splitlines():
        if line.startswith("NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def parse_uptime(text: str) -> str:
    """把 /proc/uptime 的秒数说成人话."""
    try:
        seconds = float((text or "").split()[0])
    except (ValueError, IndexError):
        return ""
    days, rem = divmod(int(seconds), 86400)
    hours = rem // 3600
    if days:
        return f"{days} 天 {hours} 小时"
    return f"{hours} 小时"


def parse_mem(text: str) -> tuple[int | None, int | None, float | None]:
    """free -m → (used, total, pct)；pct 用 total-available 算，比 used 更贴近真实压力."""
    for line in (text or "").splitlines():
        if not line.strip().lower().startswith("mem:"):
            continue
        parts = line.split()[1:]
        if len(parts) < 2:
            return None, None, None
        try:
            total = int(parts[0])
        except ValueError:
            return None, None, None
        available = None
        if len(parts) >= 6:
            try:
                available = int(parts[5])
            except ValueError:
                available = None
        if available is not None:
            used = max(0, total - available)
        else:
            try:
                used = int(parts[1])
            except ValueError:
                used = 0
        pct = round(used * 100.0 / total, 1) if total else None
        return used, total, pct
    return None, None, None


def parse_disk(text: str) -> tuple[str, str, float | None]:
    """df -h / → (used, total, pct)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return "", "", None
    parts = lines[1].split()
    if len(parts) < 5:
        return "", "", None
    total, used, pct_s = parts[1], parts[2], parts[4]
    try:
        pct = float(pct_s.rstrip("%"))
    except ValueError:
        pct = None
    return used, total, pct


def parse_load(text: str) -> float | None:
    """/proc/loadavg 第一个数就是 1 分钟负载."""
    try:
        return float((text or "").split()[0])
    except (ValueError, IndexError):
        return None


def parse_ports(text: str) -> list[str]:
    """ss -tlnH / netstat -tln → 去重后的监听端口列表（只留数字，去重排序）."""
    ports: set[int] = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("state", "proto", "active")):
            continue
        # ss: LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
        # netstat: tcp 0 0 0.0.0.0:22 0.0.0.0:* LISTEN
        for token in line.split():
            if ":" not in token:
                continue
            tail = token.rsplit(":", 1)[-1]
            if tail.isdigit():
                ports.add(int(tail))
                break
    return [f":{p}" for p in sorted(ports)]


def parse_port_details(text: str) -> list[dict]:
    """ss/netstat 输出 → [{port, addr, proc, scope}]，回答「谁在听、对外还是只对本机」.

    原来只留端口号，用户看到 `:22` 会怀疑"我明明没开 22 啊" ——
    其实是 sshd 在听，只是界面没告诉他。这几个字段就是为了消掉这个疑问。

    `scope`: "all" = 0.0.0.0 / ::（外网可达）；"local" = 127.x / ::1（只有本机能连）
    """
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("state", "proto", "active")):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        # ss:      LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=890,fd=3))
        # netstat: tcp 0 0 0.0.0.0:22 0.0.0.0:* LISTEN 890/sshd
        addr_token = ""
        for token in parts:
            if ":" in token and token.rsplit(":", 1)[-1].isdigit():
                # 跳过 peer address（形如 0.0.0.0:*）
                addr_token = token
                break
        if not addr_token:
            continue

        addr, _, port_s = addr_token.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            continue
        addr = addr.strip("[]")

        proc = ""
        m = re.search(r'\(\("([^\"]+)",pid=(\d+)', line)
        if m:
            proc = m.group(1)
        else:
            # netstat 格式：最后一列 890/sshd
            tail = parts[-1]
            if "/" in tail and not tail.endswith("*"):
                proc = tail.split("/", 1)[1]

        scope = "local" if (addr.startswith("127.") or addr in ("::1", "::ffff:127.0.0.1")) else "all"
        key = (port, addr)
        if key in seen:
            continue
        seen.add(key)
        out.append({"port": port, "addr": addr or "0.0.0.0", "proc": proc, "scope": scope})

    out.sort(key=lambda x: (x["port"], x["addr"]))
    return out


def parse_containers(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def parse(raw_stdout: str) -> Snapshot:
    """整段输出 → Snapshot."""
    sec = split_sections(raw_stdout)
    used, total, mem_pct = parse_mem(sec.get("MEM", ""))
    disk_used, disk_total, disk_pct = parse_disk(sec.get("DISK", ""))

    return Snapshot(
        hostname=sec.get("HOST", "").strip(),
        os_name=parse_os(sec.get("OS", "")),
        kernel=sec.get("KERNEL", "").strip(),
        uptime_text=parse_uptime(sec.get("UPTIME", "")),
        load1=parse_load(sec.get("LOAD", "")),
        mem_used=used,
        mem_total=total,
        mem_pct=mem_pct,
        disk_used=disk_used,
        disk_total=disk_total,
        disk_pct=disk_pct,
        containers=parse_containers(sec.get("DOCKER", "")),
        ports=parse_ports(sec.get("PORTS", "")),
        port_details=parse_port_details(sec.get("PORTS", "")),
        raw=sec,
    )
