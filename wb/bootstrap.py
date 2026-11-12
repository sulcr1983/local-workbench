"""一键发现：把本机能自动拿到的服务器全捞出来，能连的直接填好.

分工原则 —— 系统做「能做的」，人只做「只有人能做的」：

    系统能做   扫 ~/.ssh/config、known_hosts、项目文档里提到的地址；
               读已配好 AK/SK 的云账号拉实例列表；
               探测 22 端口通不通；用本机密钥试登录拿主机名；
               按 host:port 去重、跟已有服务器比对、把 user/port/key 全填好。
    人只能做   第一次填云厂商 AK/SK（凭证在人家账号里，系统变不出来）；
               给密码认证的机器补密码（密码只存在人脑子里）。

候选的三种状态，页面按这个分组显示：
    ready     密钥能登上 → 勾上直接导入，零输入
    alive     端口通但没验证 → 导入后要补密码
    dead      端口不通 → 默认不勾，留着当线索
"""

from __future__ import annotations

import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor

from . import db, discover

PROBE_TIMEOUT = 1.2   # 端口探测：1.2s 足够，太长会让整轮扫描卡住
SSH_TIMEOUT = 7       # 试连：握手 + 认证 + 一条 hostname
MAX_PROBE = 40        # 最多探这么多个，防止配了几百条地址时卡死
MAX_VERIFY = 8        # 最多试连这么多台，按有无密钥优先

SOURCE_LABEL = {
    "ssh_config": "SSH 配置",
    "known_hosts": "连过的机器",
    "project_doc": "项目文档",
    "cloud": "云账号",
}


# ------------------------------------------------------------------ 探测
def tcp_alive(host: str, port: int = 22, timeout: float = PROBE_TIMEOUT) -> bool:
    """端口能不能连上。连不上就是连不上，不重试（重试只会让页面更慢）."""
    if not host:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


# 服务器默认 MaxAuthTries=6，一次带太多密钥会直接被踢 —— 留点余量
MAX_KEYS_PER_TRY = 5

# 22 不通时探这批端口。按国内 VPS 常见的改端口习惯排序，别扫全表。
SSH_PORT_HINTS = (2222, 2022, 22022, 10022, 65032, 22222, 20022)


def _kh_items(raw) -> list[dict]:
    """known_hosts 候选统一成 [{host, port}]。

    discover 现在返回带端口的 dict；老格式（纯字符串）也认，测试和旧调用不受影响。
    """
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append({"host": str(item.get("host") or ""), "port": int(item.get("port") or 22)})
        elif item:
            out.append({"host": str(item), "port": 22})
    return out


def find_ssh_port(host: str, base: int = 22) -> int:
    """base 端口不通时，探一批常见 SSH 端口，返回第一个通的.

    很多人把 sshd 改到 2222（一键脚本默认），而 known_hosts 里记的是 `[host]:2222`。
    只探固定的一小撮端口，且只在原端口不通时才做 —— 不当端口扫描器用。
    并发探，最坏 1.2s 一轮；串行要 8s 以上，一键发现会卡成 PPT。
    """
    if not host:
        return base
    if tcp_alive(host, base, timeout=1.2):
        return base
    ports = [p for p in SSH_PORT_HINTS if p != base]
    with ThreadPoolExecutor(max_workers=len(ports) or 1) as pool:
        hits = list(pool.map(lambda p: (p, tcp_alive(host, p, timeout=1.2)), ports))
    for port, ok in hits:  # map 保序，所以拿到的是候选优先级最高的那个
        if ok:
            return port
    return base


def _split_keys(secret: str) -> list[str]:
    """secret 里可能是多把私钥（换行或分号分隔），拆成存在的文件列表.

    这里**不截断** —— 截断会把老密钥永久藏起来（113 靠的是第 1 把 deploy_key，
    但换台机器可能要第 7 把）。截断的工作交给 `_key_batches` 分批做。
    """
    paths = [p.strip() for p in str(secret or "").replace(";", "\n").splitlines() if p.strip()]
    return [p for p in paths if os.path.isfile(p)]


def _key_batches(keys: list[str], size: int = MAX_KEYS_PER_TRY) -> list[list[str]]:
    """按 MaxAuthTries 分批。每批一条独立连接，认证计数会重新算."""
    return [keys[i:i + size] for i in range(0, len(keys), max(1, size))]


def _ssh_try(host: str, port: int, user: str, keys: list[str], legacy: bool) -> tuple[bool, str]:
    from . import ssh as sshmod

    try:
        client = sshmod.open_session(
            {
                "host": host, "port": port, "user": user,
                "auth": "key_path", "secret": "\n".join(keys), "legacy_kex": legacy,
            },
            timeout=SSH_TIMEOUT,
        )
    except Exception as err:  # noqa: BLE001 - 连接失败种类多，一律只取第一行
        return False, str(err).splitlines()[0][:100]
    try:
        out, _err, _code = sshmod.run(client, "hostname", timeout=SSH_TIMEOUT)
        lines = (out or "").strip().splitlines()
        return True, (lines[0][:60] if lines else "")
    except Exception as err:  # noqa: BLE001
        return False, str(err).splitlines()[0][:100]
    finally:
        sshmod.close(client)


def _ssh_try_cli(host: str, port: int, user: str, keys: list[str]) -> tuple[bool, str]:
    """paramiko 认不了的新算法（sntrup761x25519）交给系统 ssh."""
    from . import ssh as sshmod

    try:
        out, _err, code = sshmod.run_via_cli(
            {"host": host, "port": port, "user": user, "auth": "key_path",
             "secret": "\n".join(keys)},
            "hostname",
        )
    except Exception as err:  # noqa: BLE001
        return False, str(err).splitlines()[0][:100]
    lines = (out or "").strip().splitlines()
    if code == 0 and lines:
        return True, lines[0][:60]
    return False, "系统 ssh 也连不上"


def _is_handshake_error(info: str) -> bool:
    """握手 / 算法谈不拢。这种错换密钥没用，得换客户端."""
    low = (info or "").lower()
    return any(k in low for k in ("算法", "kex", "negotiat", "握手", "no matching"))


def _degrade(host: str, port: int, user: str, keys: list[str], first: str) -> tuple[bool, str]:
    """常规 paramiko 谈不动时的两级后备：老 kex → 系统 ssh."""
    ok, info = _ssh_try(host, port, user, keys, legacy=True)
    if ok:
        return True, info
    from . import ssh as sshmod

    if sshmod.cli_available():
        ok, info = _ssh_try_cli(host, port, user, keys)
        if ok:
            return True, info
        return False, info or first
    return False, first


def ssh_probe(host: str, port: int, user: str, key_path: str) -> tuple[bool, str]:
    """试一次公钥登录，跑只读的 hostname。逐批密钥试，握手不通就降级。

    返回 (成功?, 主机名 或 失败原因)。失败不抛，调用方只看布尔值。
    """
    keys = _split_keys(key_path)
    if not keys:
        return False, "没有可用的私钥"

    last = ""
    for batch in _key_batches(keys):
        ok, info = _ssh_try(host, port, user, batch, legacy=False)
        if ok:
            return True, info
        last = info
        # 握手问题与密钥无关（哪把都过不去），直接降级，别白试剩下几批
        if _is_handshake_error(info):
            return _degrade(host, port, user, batch, info)
    return False, last


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _local_keys() -> list[str]:
    """本机可用的私钥文件路径（只读文件头判断类型，内容一个字节都不解析）.

    不能只看 `id_*` 命名 —— 有人会起名 `deploy_key`、`xxx_key`，
    某台服务器就是用 deploy_key 这把钥匙。

    排序按修改时间**新 → 旧**：刚生成的钥匙最可能是给新机器用的，
    先试命中率高，也省掉每台机器多试几轮的开销。
    """
    found = []
    if not os.path.isdir(discover.SSH_DIR):
        return found
    skip = {"config", "known_hosts", "known_hosts.old", "authorized_keys", "environment"}
    try:
        names = sorted(os.listdir(discover.SSH_DIR))
    except OSError:
        return found
    for name in names:
        if name.endswith(".pub") or name in skip or name.startswith("."):
            continue
        full = os.path.join(discover.SSH_DIR, name)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "rb") as f:
                head = f.read(48)
        except OSError:
            continue
        if b"PRIVATE KEY" in head:
            found.append(full)
    found.sort(key=_mtime, reverse=True)
    return found


# ------------------------------------------------------------------ 云账号
def _from_cloud() -> tuple[list[dict], list[str]]:
    """已配好的云账号 → 实例列表。凭证是人填的，这里只负责用."""
    from .expiry.providers import aliyun, custom, huawei, tencent

    mods = {"tencent": tencent, "aliyun": aliyun, "huawei": huawei, "custom": custom}
    items: list[dict] = []
    warns: list[str] = []

    for row in db.query("SELECT * FROM provider WHERE enabled=1 ORDER BY id"):
        mod = mods.get(row["kind"])
        if mod is None:
            continue
        try:
            cfg = json.loads(row["cfg"] or "{}")
        except json.JSONDecodeError:
            warns.append(f"{row['name']}：配置不是合法 JSON，跳过")
            continue
        try:
            fetched = mod.fetch(cfg)
        except Exception as err:  # noqa: BLE001 - provider 自己会转中文，兜一层
            warns.append(f"{row['name']}：{str(err).splitlines()[0][:100]}")
            continue
        for it in fetched:
            host = str(it.get("host") or "").strip()
            if not host:
                continue
            items.append(
                {
                    "host": host,
                    "port": 22,
                    "user": "root",
                    "auth": "password",
                    "secret": "",
                    "name": str(it.get("name") or host)[:80],
                    "source": "cloud",
                    "detail": f"{row['name']}",
                    "note": str(it.get("note") or "")[:80],
                }
            )
    return items, warns


# ------------------------------------------------------------------ 汇总
def gather(roots: list[str] | None = None, probe: bool = True, verify: bool = True) -> dict:
    """把所有线索汇总成候选列表。返回 {items, warnings, stats}."""
    raw: dict[str, dict] = {}   # host:port → 候选
    warns: list[str] = []

    def add(item: dict) -> None:
        key = f"{item['host']}:{item['port']}"
        old = raw.get(key)
        if old is None:
            raw[key] = item
            return
        # 同一台机器被多处提到：保留信息更全的那份（有 user/key 的优先）
        better = bool(item.get("secret")) and not old.get("secret")
        if better:
            item["detail"] = f"{old['detail']} / {item['detail']}"
            raw[key] = item
        else:
            old["detail"] = f"{old['detail']} / {item['detail']}"

    ssh = discover.ssh_candidates()
    for c in ssh.get("config", []):
        keys = c.get("keys") or ([c["key"]] if c.get("key") else [])
        add(
            {
                "host": c["host"], "port": int(c.get("port") or 22),
                "user": c.get("user") or "root",
                "auth": "key_path" if keys else "password",
                # 多把私钥换行拼起来，认证时逐个试 —— 主密钥和兜底密钥都可能有用
                "secret": "\n".join(keys),
                "name": c.get("alias") or c["host"],
                "source": "ssh_config", "detail": c.get("alias") or "", "note": "",
                "keys": keys,
            }
        )

    keys = _local_keys()
    for kh in _kh_items(ssh.get("known_hosts", [])):
        host, port = kh["host"], kh["port"]
        add(
            {
                "host": host, "port": port, "user": "root",
                "auth": "key_path" if keys else "password",
                # 不知道这台用哪把钥匙，就全带上让认证层逐个试（113 用的是 deploy_key）
                "secret": "\n".join(keys),
                "name": host,
                "source": "known_hosts", "detail": "", "note": "",
                "keys": keys,
            }
        )

    if roots:
        try:
            for h in discover.project_hosts(roots):
                add(
                    {
                        "host": h["host"], "port": int(h.get("port") or 22),
                        "user": h.get("user") or "root", "auth": "password", "secret": "",
                        "name": h["host"], "source": "project_doc",
                        "detail": h.get("from") or "", "note": "",
                    }
                )
        except Exception as err:  # noqa: BLE001
            warns.append(f"扫项目文档时出错：{str(err).splitlines()[0][:80]}")

    cloud_items, cloud_warns = _from_cloud()
    warns.extend(cloud_warns)
    for item in cloud_items:
        add(item)

    items = list(raw.values())[:MAX_PROBE]

    # 已在册的标出来，避免导重复
    have = {(r["host"], int(r["port"])) for r in db.query("SELECT host, port FROM server")}
    for it in items:
        it["exists"] = (it["host"], int(it["port"])) in have
        it["alive"] = None
        it["verified"] = False
        it["hostname"] = ""
        it["why"] = ""

    if probe and items:

        def _probe(item: dict) -> tuple[bool, int]:
            port = int(item.get("port") or 22)
            if tcp_alive(item["host"], port):
                return True, port
            # 只有默认端口失败时才扩展探测，别把它当端口扫描器用
            if port == 22:
                found = find_ssh_port(item["host"], 22)
                if found != 22:
                    return True, found
            return False, port

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(_probe, items))
        for it, (ok, port) in zip(items, results):
            it["alive"] = ok
            if ok and port != int(it.get("port") or 22):
                it["port_fixed"] = f"22 不通，实际在 {port}"
                it["port"] = port

    # 有密钥 + 端口通的，试连一次拿主机名 —— 成功就是「零输入可导入」
    if verify and items:
        targets = [i for i in items if i["alive"] and i.get("secret")][:MAX_VERIFY]
        if targets:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(
                    pool.map(lambda i: ssh_probe(i["host"], i["port"], i["user"], i["secret"]), targets)
                )
            for it, (ok, info) in zip(targets, results):
                it["verified"] = ok
                if ok:
                    it["hostname"] = info
                    if info and it["name"] in (it["host"], "", None):
                        it["name"] = info
                else:
                    it["why"] = info

    for it in items:
        if it["verified"]:
            it["status"] = "ready"
        elif it["alive"]:
            it["status"] = "alive"
        else:
            it["status"] = "dead"
        if it["exists"]:
            it["status"] = "exists"

    order = {"ready": 0, "alive": 1, "exists": 2, "dead": 3}
    items.sort(key=lambda i: (order.get(i["status"], 9), i["name"].lower()))

    stats = {
        "total": len(items),
        "ready": sum(1 for i in items if i["status"] == "ready"),
        "alive": sum(1 for i in items if i["status"] == "alive"),
        "exists": sum(1 for i in items if i["status"] == "exists"),
        "dead": sum(1 for i in items if i["status"] == "dead"),
        "cloud": sum(1 for i in items if i["source"] == "cloud"),
    }
    return {"items": items, "warnings": warns, "stats": stats}


# ------------------------------------------------------------------ 导入
def import_items(items: list[dict]) -> dict:
    """批量入库。只收白名单字段，同 host:port 不重复插."""
    allowed = {"name", "host", "port", "user", "auth", "secret", "note"}
    added, skipped, updated = 0, 0, 0

    for item in items:
        data = {k: item.get(k) for k in allowed if item.get(k) not in (None, "")}
        host = str(data.get("host") or "").strip()
        if not host:
            skipped += 1
            continue
        port = int(data.get("port") or 22)
        row = db.query_one("SELECT id, secret FROM server WHERE host=? AND port=?", (host, port))
        if row is not None:
            # 已存在但凭据不一样：只有「新的是验证过的」才替换 ——
            # 之前一律跳过，导致猜错的密钥永远修不好（113 存的是 fcbox，实际用 deploy_key）
            new_secret = str(data.get("secret") or "")
            if item.get("verified") and new_secret and new_secret != (row["secret"] or ""):
                with db.tx() as c:
                    c.execute(
                        "UPDATE server SET auth=?, secret=?, user=?, updated_at=? WHERE id=?",
                        (data.get("auth") or "key_path", new_secret,
                         str(data.get("user") or "root"), db.now(), row["id"]),
                    )
                updated += 1
            else:
                skipped += 1
            continue

        auth = data.get("auth") or "password"
        secret = str(data.get("secret") or "")
        # 密码认证又没密码的，先占个位，标成待补 —— 不静默丢掉，也不假装能连
        note = str(data.get("note") or "")
        if auth == "password" and not secret:
            note = (note + " · 待补密码").strip(" ·")

        with db.tx() as c:
            c.execute(
                """INSERT INTO server(name, host, port, user, auth, secret, note,
                                      enabled, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,1,?,?)""",
                (
                    str(data.get("name") or host)[:80], host, port,
                    str(data.get("user") or "root"), auth, secret, note[:200],
                    db.now(), db.now(),
                ),
            )
        added += 1

    return {"added": added, "skipped": skipped, "updated": updated}
