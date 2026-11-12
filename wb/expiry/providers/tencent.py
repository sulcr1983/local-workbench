"""腾讯云 CVM 到期采集。

cfg = {"secret_id": ..., "secret_key": ..., "regions": ["ap-guangzhou", ...]}

SDK 一律在 `_raw_instances` 里延迟导入：没装包的环境 import 本模块不能失败。
"""

from __future__ import annotations

import json

from .custom import ProviderError, _regions, _scrub, _to_bool, _to_date

KIND = "tencent"
LABEL = "腾讯云"
DEFAULT_REGIONS = ("ap-guangzhou",)
PAGE_LIMIT = 100  # 单区域最多取 100 台，够用；不做翻页


def _raw_instances(cfg: dict, region: str) -> list[dict]:
    """拉一个区域的原始实例 JSON（InstanceSet）."""
    from tencentcloud.common import credential
    from tencentcloud.cvm.v20170312 import cvm_client, models

    cred = credential.Credential(cfg["secret_id"], cfg["secret_key"])
    client = cvm_client.CvmClient(cred, region)

    req = models.DescribeInstancesRequest()
    req.from_json_string(json.dumps({"Limit": PAGE_LIMIT}))
    resp = client.DescribeInstances(req)
    data = json.loads(resp.to_json_string() or "{}")
    return list(data.get("InstanceSet") or [])


def _map_instance(raw: dict, region: str) -> dict:
    """原始实例 → 统一结构."""
    name = str(raw.get("InstanceName") or raw.get("InstanceId") or "(未命名实例)").strip()
    expire = _to_date(raw.get("DeadlineTime"))
    renew = _to_bool(raw.get("AutoRenew"))

    note = f"{region} · " + ("自动续费" if renew else "未自动续费")
    if "AutoRenew" not in raw:
        note = f"{region} · 接口未返回自动续费状态"
    if not expire:
        note += " · 接口未返回到期时间"

    # 公网 IP 优先 —— 工作台的服务器巡检要能连上它
    host = ""
    for key in ("PublicIpAddresses", "PrivateIpAddresses"):
        ips = raw.get(key) or []
        if ips:
            host = str(ips[0]).strip()
            break

    return {"name": name[:120], "expire": expire, "renew": renew,
            "host": host, "note": note}


def fetch(cfg: dict) -> list[dict]:
    """按区域遍历；单个区域失败不牵连其他区域，原因写进 note."""
    secret_id = str(cfg.get("secret_id") or "").strip()
    secret_key = str(cfg.get("secret_key") or "").strip()
    if not secret_id or not secret_key:
        raise ProviderError("腾讯云未填写 SecretId / SecretKey")

    regions, defaulted = _regions(cfg, DEFAULT_REGIONS)
    out: list[dict] = []
    failed: list[str] = []

    for region in regions:
        try:
            raws = _raw_instances(cfg, region)
        except Exception as err:  # noqa: BLE001 - SDK 异常种类多，一律转中文
            failed.append(f"{region}：{_scrub(err, secret_id, secret_key)}")
            continue
        for raw in raws:
            out.append(_map_instance(raw, region))

    if failed and not out:
        raise ProviderError("腾讯云接口报错：" + "；".join(failed))
    if failed:
        tail = "；区域失败 — " + "；".join(failed)
        for item in out:
            item["note"] += tail
    if defaulted:
        for item in out:
            item["note"] += f"；未填区域，默认只查 {regions[0]}"
    return out
