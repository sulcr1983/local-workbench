"""阿里云 ECS 到期采集。

cfg = {"access_key_id": ..., "access_key_secret": ..., "regions": ["cn-hangzhou", ...]}

SDK 一律在 `_raw_instances` 里延迟导入。
"""

from __future__ import annotations

from .custom import ProviderError, _regions, _scrub, _to_bool, _to_date

KIND = "aliyun"
LABEL = "阿里云"
DEFAULT_REGIONS = ("cn-hangzhou",)
PAGE_SIZE = 100  # 单区域最多取 100 台，不做翻页


def _raw_instances(cfg: dict, region: str) -> list[dict]:
    """拉一个区域的原始实例列表（Instances.Instance）."""
    from alibabacloud_ecs20140526.client import Client
    from alibabacloud_ecs20140526 import models
    from alibabacloud_tea_openapi.models import Config

    config = Config(
        access_key_id=cfg["access_key_id"],
        access_key_secret=cfg["access_key_secret"],
        endpoint=f"ecs.{region}.aliyuncs.com",
    )
    client = Client(config)

    req = models.DescribeInstancesRequest(region_id=region, page_size=PAGE_SIZE)
    resp = client.describe_instances(req)
    body = resp.body.to_map() if hasattr(resp.body, "to_map") else {}
    return list((body.get("Instances") or {}).get("Instance") or [])


def _map_instance(raw: dict, region: str) -> dict:
    """原始实例 → 统一结构。DescribeInstances 不返回到期续费标记，renew 恒为 False."""
    name = str(raw.get("InstanceName") or raw.get("InstanceId") or "(未命名实例)").strip()
    expire = _to_date(raw.get("ExpiredTime"))
    renew = _to_bool(raw.get("AutoRenew"))

    if "AutoRenew" in raw:
        note = f"{region} · " + ("自动续费" if renew else "未自动续费")
    else:
        note = f"{region} · 接口未返回自动续费状态"
    if not expire:
        note += " · 接口未返回到期时间"

    host = ""
    public = (raw.get("PublicIpAddress") or {}).get("IpAddress") or []
    private = ((raw.get("VpcAttributes") or {}).get("PrivateIpAddress") or {}).get("IpAddress") or []
    for ips in (public, private):
        if ips:
            host = str(ips[0]).strip()
            break

    return {"name": name[:120], "expire": expire, "renew": renew,
            "host": host, "note": note}


def fetch(cfg: dict) -> list[dict]:
    """按区域遍历；单个区域失败不牵连其他区域，原因写进 note."""
    ak = str(cfg.get("access_key_id") or "").strip()
    sk = str(cfg.get("access_key_secret") or "").strip()
    if not ak or not sk:
        raise ProviderError("阿里云未填写 AccessKey ID / AccessKey Secret")

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
        raise ProviderError("阿里云接口报错：" + "；".join(failed))
    if failed:
        tail = "；区域失败 — " + "；".join(failed)
        for item in out:
            item["note"] += tail
    if defaulted:
        for item in out:
            item["note"] += f"；未填区域，默认只查 {regions[0]}"
    return out
