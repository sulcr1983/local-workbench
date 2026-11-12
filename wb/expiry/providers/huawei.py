"""华为云 ECS 到期采集。

cfg = {"ak": ..., "sk": ..., "project_id": ..., "regions": ["cn-north-4", ...]}

华为云的鉴权必须带 project_id，缺了就只能报可读错误，不能静默失败。
SDK 一律在 `_raw_instances` 里延迟导入。
"""

from __future__ import annotations

from .custom import ProviderError, _regions, _scrub, _to_bool, _to_date

KIND = "huawei"
LABEL = "华为云"
DEFAULT_REGIONS = ("cn-north-4",)
PAGE_LIMIT = 100  # 单区域最多取 100 台，不做翻页


def _raw_instances(cfg: dict, region: str) -> list[dict]:
    """拉一个区域的原始服务器列表（servers）."""
    from huaweicloudsdkcore.auth.credentials import BasicCredentials
    from huaweicloudsdkecs.v2 import EcsClient, ListServersDetailsRequest

    cred = BasicCredentials(cfg["ak"], cfg["sk"], cfg["project_id"])
    client = (
        EcsClient.new_builder()
        .with_credentials(cred)
        .with_endpoint(f"https://ecs.{region}.myhuaweicloud.com")
        .build()
    )
    resp = client.list_servers_details(ListServersDetailsRequest(limit=PAGE_LIMIT))
    out = []
    for server in resp.servers or []:
        out.append(server.to_dict() if hasattr(server, "to_dict") else dict(server))
    return out


def _map_instance(raw: dict, region: str) -> dict:
    """原始服务器 → 统一结构。ListServersDetails 不返回到期时间时明确写进 note."""
    name = str(raw.get("name") or raw.get("id") or "(未命名实例)").strip()
    expire = _to_date(raw.get("expire_time"))
    renew = _to_bool(raw.get("auto_renew"))

    note = f"{region} · " + ("自动续费" if renew else "未返回自动续费状态")
    if not expire:
        note += " · 该实例未返回到期时间"

    host = ""
    for pub in raw.get("publicips") or []:
        ip = pub.get("public_ip_address") if isinstance(pub, dict) else None
        if ip:
            host = str(ip).strip()
            break
    if not host:
        for addrs in (raw.get("addresses") or {}).values():
            for a in addrs or []:
                if isinstance(a, dict) and a.get("addr"):
                    host = str(a["addr"]).strip()
                    break
            if host:
                break

    return {"name": name[:120], "expire": expire, "renew": renew,
            "host": host, "note": note}


def fetch(cfg: dict) -> list[dict]:
    """按区域遍历；单个区域失败不牵连其他区域，原因写进 note."""
    ak = str(cfg.get("ak") or "").strip()
    sk = str(cfg.get("sk") or "").strip()
    project_id = str(cfg.get("project_id") or "").strip()
    if not ak or not sk:
        raise ProviderError("华为云未填写 AK / SK")
    if not project_id:
        raise ProviderError("华为云需要 project_id，请在控制台「我的凭证」里复制项目 ID")

    regions, defaulted = _regions(cfg, DEFAULT_REGIONS)
    out: list[dict] = []
    failed: list[str] = []

    for region in regions:
        try:
            raws = _raw_instances(cfg, region)
        except Exception as err:  # noqa: BLE001 - SDK 异常种类多，一律转中文
            failed.append(f"{region}：{_scrub(err, ak, sk)}")
            continue
        for raw in raws:
            out.append(_map_instance(raw, region))

    if failed and not out:
        raise ProviderError("华为云接口报错：" + "；".join(failed))
    if failed:
        tail = "；区域失败 — " + "；".join(failed)
        for item in out:
            item["note"] += tail
    if defaulted:
        for item in out:
            item["note"] += f"；未填区域，默认只查 {regions[0]}"
    return out
