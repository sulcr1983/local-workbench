"""T12 云厂商 provider 验收（全部打桩，不依赖真实账号）.

四家的 SDK 都是延迟导入，本机没装 SDK 也能测：统一 mock 模块内的 `_raw_instances`，
再单独覆盖映射函数 `_map_instance`，这样区域遍历 + 字段归一化都是真代码在跑。
"""

from __future__ import annotations

import json
from unittest import mock

from wb.expiry.providers import aliyun, custom, huawei, tencent
from wb.expiry.providers.custom import ProviderError


class _Resp:
    """假的 requests.Response：只用到 status_code / text."""

    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _sample_json(items: list[dict]) -> str:
    return json.dumps({"code": 0, "data": {"items": items}}, ensure_ascii=False)


# ------------------------------------------------------------- KIND / LABEL
def test_kinds_and_labels():
    assert (tencent.KIND, tencent.LABEL) == ("tencent", "腾讯云")
    assert (aliyun.KIND, aliyun.LABEL) == ("aliyun", "阿里云")
    assert (huawei.KIND, huawei.LABEL) == ("huawei", "华为云")
    assert (custom.KIND, custom.LABEL) == ("custom", "自定义 API")


def test_provider_error_is_shared_class():
    for mod in (tencent, aliyun, huawei, custom):
        assert mod.ProviderError is ProviderError


# ------------------------------------------------------------------- _dig
def test_dig_dict_keys():
    assert custom._dig({"data": {"items": [1, 2]}}, "data.items") == [1, 2]


def test_dig_nested_keys():
    assert custom._dig({"a": {"b": {"c": "命中"}}}, "a.b.c") == "命中"


def test_dig_list_index():
    obj = {"list": [{"name": "第一台"}, {"name": "第二台"}]}
    assert custom._dig(obj, "list.0.name") == "第一台"
    assert custom._dig(obj, "list.1.name") == "第二台"


def test_dig_missing_returns_none():
    assert custom._dig({"a": 1}, "a.b.c") is None
    assert custom._dig({"a": 1}, "nope") is None
    assert custom._dig({"list": [1]}, "list.5") is None
    assert custom._dig(None, "a.b") is None


def test_dig_empty_path_returns_root():
    obj = {"items": [1]}
    assert custom._dig(obj, "") is obj
    assert custom._dig(obj, None) is obj


def test_dig_mixed_path_with_index():
    obj = {"data": [{"items": [{"name": "x"}]}]}
    assert custom._dig(obj, "data.0.items.0.name") == "x"


# ---------------------------------------------------------------- custom
def test_custom_fetch_maps_fields():
    items = [
        {"name": "prod-web", "expire_time": "2027-01-02T03:04:05Z", "auto_renew": True},
        {"name": "prod-db", "expire_time": "2026-12-31 00:00:00", "auto_renew": "false"},
    ]
    cfg = {"url": "https://api.example.com/x", "items_path": "data.items"}
    with mock.patch("requests.get", return_value=_Resp(_sample_json(items))):
        rows = custom.fetch(cfg)

    assert [r["name"] for r in rows] == ["prod-web", "prod-db"]
    assert [r["expire"] for r in rows] == ["2027-01-02", "2026-12-31"]
    assert [r["renew"] for r in rows] == [True, False]
    assert all(isinstance(r["note"], str) for r in rows)


def test_custom_fetch_custom_paths():
    items = [{"label": "备份机", "deadline": "2028/03/04 12:00:00", "auto": 1}]
    cfg = {
        "url": "https://api.example.com/x",
        "items_path": "",
        "name_path": "label",
        "expire_path": "deadline",
        "renew_path": "auto",
    }
    with mock.patch("requests.get", return_value=_Resp(json.dumps(items, ensure_ascii=False))):
        rows = custom.fetch(cfg)

    assert rows == [{"name": "备份机", "expire": "2028-03-04", "renew": True, "note": "自动续费"}]


def test_custom_fetch_requests_timeout_12s():
    cfg = {"url": "https://api.example.com/x", "items_path": "data.items"}
    with mock.patch("requests.get", return_value=_Resp(_sample_json([]))) as get:
        custom.fetch(cfg)
    assert get.call_args.kwargs["timeout"] == 12


def test_custom_preview_limits_to_three():
    items = [
        {"name": f"vm-{i}", "expire_time": "2027-01-01", "auto_renew": False}
        for i in range(5)
    ]
    cfg = {"url": "https://api.example.com/x", "items_path": "data.items"}
    with mock.patch("requests.get", return_value=_Resp(_sample_json(items))):
        rows = custom.preview(cfg)
    assert len(rows) == 3
    assert [r["name"] for r in rows] == ["vm-0", "vm-1", "vm-2"]


def test_custom_fetch_http_500_raises():
    cfg = {"url": "https://api.example.com/x", "items_path": "data.items"}
    with mock.patch("requests.get", return_value=_Resp("oops", status_code=500)):
        try:
            custom.fetch(cfg)
        except ProviderError as err:
            msg = str(err)
            assert "500" in msg, msg
            assert "自定义 API" in msg, msg
        else:
            raise AssertionError("HTTP 500 应当抛 ProviderError")


def test_custom_fetch_non_json_raises():
    cfg = {"url": "https://api.example.com/x", "items_path": "data.items"}
    with mock.patch("requests.get", return_value=_Resp("<html>网关错误</html>")):
        try:
            custom.fetch(cfg)
        except ProviderError as err:
            assert "不是 JSON" in str(err), str(err)
        else:
            raise AssertionError("非 JSON 应当抛 ProviderError")


def test_custom_fetch_missing_expire_raises():
    items = [{"name": "prod-web"}]
    cfg = {"url": "https://api.example.com/x", "items_path": "data.items"}
    with mock.patch("requests.get", return_value=_Resp(_sample_json(items))):
        try:
            custom.fetch(cfg)
        except ProviderError as err:
            assert "缺少到期时间" in str(err), str(err)
        else:
            raise AssertionError("字段缺失应当抛 ProviderError")


def test_custom_fetch_missing_name_raises():
    items = [{"expire_time": "2027-01-01"}]
    cfg = {"url": "https://api.example.com/x", "items_path": "data.items"}
    with mock.patch("requests.get", return_value=_Resp(_sample_json(items))):
        try:
            custom.fetch(cfg)
        except ProviderError as err:
            assert "缺少名称" in str(err), str(err)
        else:
            raise AssertionError("字段缺失应当抛 ProviderError")


def test_custom_fetch_items_path_not_list_raises():
    cfg = {"url": "https://api.example.com/x", "items_path": "data"}
    with mock.patch("requests.get", return_value=_Resp(_sample_json([]))):
        try:
            custom.fetch(cfg)
        except ProviderError as err:
            assert "不是列表" in str(err), str(err)
        else:
            raise AssertionError("items_path 不是列表应当抛 ProviderError")


def test_custom_fetch_network_error_raises():
    cfg = {"url": "https://api.example.com/x", "items_path": "data.items"}
    with mock.patch("requests.get", side_effect=OSError("connection refused")):
        try:
            custom.fetch(cfg)
        except ProviderError as err:
            assert "请求失败" in str(err), str(err)
        else:
            raise AssertionError("网络异常应当抛 ProviderError")


def test_custom_fetch_no_url_raises():
    try:
        custom.fetch({"items_path": "data.items"})
    except ProviderError as err:
        assert "URL" in str(err), str(err)
    else:
        raise AssertionError("缺 URL 应当抛 ProviderError")


def test_custom_error_does_not_leak_token():
    """响应体里回显了 token，错误信息必须抹掉它."""
    token = "sk-live-9f8e7d6c5b4a"
    cfg = {
        "url": "https://api.example.com/x",
        "headers": {"Authorization": f"Bearer {token}"},
        "items_path": "data.items",
    }
    body = f'{{"echo":"Bearer {token}" 这不是 JSON'
    with mock.patch("requests.get", return_value=_Resp(body)):
        try:
            custom.fetch(cfg)
        except ProviderError as err:
            assert token not in str(err), str(err)
            assert "***" in str(err), str(err)
        else:
            raise AssertionError("非法 JSON 应当抛 ProviderError")


# --------------------------------------------------------------- tencent
def test_tencent_maps_instances():
    raws = [
        {
            "InstanceId": "ins-1",
            "InstanceName": "web-1",
            "DeadlineTime": "2027-03-04T00:00:00+08:00",
            "AutoRenew": "true",
        },
        {"InstanceId": "ins-2", "DeadlineTime": "2027-04-05T00:00:00+08:00"},
    ]
    cfg = {"secret_id": "id", "secret_key": "key", "regions": ["ap-guangzhou"]}
    with mock.patch.object(tencent, "_raw_instances", return_value=raws):
        rows = tencent.fetch(cfg)

    assert [r["name"] for r in rows] == ["web-1", "ins-2"]
    assert [r["expire"] for r in rows] == ["2027-03-04", "2027-04-05"]
    assert [r["renew"] for r in rows] == [True, False]
    assert "ap-guangzhou" in rows[0]["note"]
    assert "未返回自动续费状态" in rows[1]["note"]


def test_tencent_one_region_failure_isolated():
    def fake(cfg, region):
        if region == "ap-shanghai":
            raise RuntimeError("RequestLimitExceeded")
        return [{"InstanceId": "ins-1", "DeadlineTime": "2027-03-04 00:00:00"}]

    cfg = {"secret_id": "id", "secret_key": "key", "regions": ["ap-guangzhou", "ap-shanghai"]}
    with mock.patch.object(tencent, "_raw_instances", side_effect=fake):
        rows = tencent.fetch(cfg)

    assert len(rows) == 1
    assert rows[0]["name"] == "ins-1"
    assert "ap-shanghai" in rows[0]["note"]
    assert "RequestLimitExceeded" in rows[0]["note"]


def test_tencent_all_regions_fail_raises():
    cfg = {"secret_id": "id", "secret_key": "key", "regions": ["ap-guangzhou"]}
    with mock.patch.object(tencent, "_raw_instances", side_effect=RuntimeError("AuthFailure")):
        try:
            tencent.fetch(cfg)
        except ProviderError as err:
            assert "腾讯云接口报错" in str(err), str(err)
        else:
            raise AssertionError("全区域失败应当抛 ProviderError")


def test_tencent_default_region_noted():
    cfg = {"secret_id": "id", "secret_key": "key"}
    with mock.patch.object(tencent, "_raw_instances", return_value=[{"InstanceId": "ins-1"}]):
        rows = tencent.fetch(cfg)
    assert rows and "ap-guangzhou" in rows[0]["note"]
    assert "未填区域" in rows[0]["note"]


def test_tencent_missing_credentials_raises():
    try:
        tencent.fetch({"regions": ["ap-guangzhou"]})
    except ProviderError as err:
        assert "SecretId" in str(err), str(err)
    else:
        raise AssertionError("缺密钥应当抛 ProviderError")


def test_tencent_error_does_not_leak_secret_key():
    secret_key = "AKIDsecret-9f8e7d6c5b4a"
    cfg = {"secret_id": "id-001", "secret_key": secret_key, "regions": ["ap-guangzhou"]}
    with mock.patch.object(
        tencent, "_raw_instances", side_effect=RuntimeError(f"AuthFailure secret_key={secret_key}")
    ):
        try:
            tencent.fetch(cfg)
        except ProviderError as err:
            assert secret_key not in str(err), str(err)
            assert "腾讯云接口报错" in str(err), str(err)
        else:
            raise AssertionError("鉴权失败应当抛 ProviderError")


# ---------------------------------------------------------------- aliyun
def test_aliyun_maps_instances():
    raws = [
        {
            "InstanceId": "i-1",
            "InstanceName": "ali-web",
            "ExpiredTime": "2027-05-06T00:00Z",
            "AutoRenew": "false",
        },
        {"InstanceId": "i-2", "ExpiredTime": "2027-06-07T00:00Z"},
    ]
    cfg = {
        "access_key_id": "ak",
        "access_key_secret": "sk",
        "regions": ["cn-hangzhou"],
    }
    with mock.patch.object(aliyun, "_raw_instances", return_value=raws):
        rows = aliyun.fetch(cfg)

    assert [r["name"] for r in rows] == ["ali-web", "i-2"]
    assert [r["expire"] for r in rows] == ["2027-05-06", "2027-06-07"]
    assert [r["renew"] for r in rows] == [False, False]
    assert "cn-hangzhou" in rows[0]["note"]


def test_aliyun_one_region_failure_isolated():
    def fake(cfg, region):
        if region == "cn-beijing":
            raise RuntimeError("InvalidAccessKeyId.NotFound")
        return [{"InstanceId": "i-1", "ExpiredTime": "2027-05-06T00:00Z"}]

    cfg = {
        "access_key_id": "ak",
        "access_key_secret": "sk",
        "regions": ["cn-hangzhou", "cn-beijing"],
    }
    with mock.patch.object(aliyun, "_raw_instances", side_effect=fake):
        rows = aliyun.fetch(cfg)

    assert len(rows) == 1
    assert rows[0]["name"] == "i-1"
    assert "cn-beijing" in rows[0]["note"]
    assert "InvalidAccessKeyId" in rows[0]["note"]


def test_aliyun_missing_credentials_raises():
    try:
        aliyun.fetch({"regions": ["cn-hangzhou"]})
    except ProviderError as err:
        assert "AccessKey" in str(err), str(err)
    else:
        raise AssertionError("缺密钥应当抛 ProviderError")


# ---------------------------------------------------------------- huawei
def test_huawei_maps_instances():
    raws = [
        {"id": "srv-1", "name": "hw-web", "expire_time": "2027-07-08T00:00:00Z"},
        {"id": "srv-2", "name": "hw-db"},
    ]
    cfg = {"ak": "ak", "sk": "sk", "project_id": "pid", "regions": ["cn-north-4"]}
    with mock.patch.object(huawei, "_raw_instances", return_value=raws):
        rows = huawei.fetch(cfg)

    assert [r["name"] for r in rows] == ["hw-web", "hw-db"]
    assert [r["expire"] for r in rows] == ["2027-07-08", ""]
    assert "cn-north-4" in rows[0]["note"]
    assert "该实例未返回到期时间" in rows[1]["note"]


def test_huawei_one_region_failure_isolated():
    def fake(cfg, region):
        if region == "cn-east-3":
            raise RuntimeError("APIGW.0301 鉴权失败")
        return [{"id": "srv-1", "name": "hw-web", "expire_time": "2027-07-08"}]

    cfg = {"ak": "ak", "sk": "sk", "project_id": "pid", "regions": ["cn-north-4", "cn-east-3"]}
    with mock.patch.object(huawei, "_raw_instances", side_effect=fake):
        rows = huawei.fetch(cfg)

    assert len(rows) == 1
    assert "cn-east-3" in rows[0]["note"]
    assert "鉴权失败" in rows[0]["note"]


def test_huawei_requires_project_id():
    cfg = {"ak": "ak", "sk": "sk", "regions": ["cn-north-4"]}
    try:
        huawei.fetch(cfg)
    except ProviderError as err:
        assert "project_id" in str(err), str(err)
    else:
        raise AssertionError("缺 project_id 应当抛 ProviderError")


def test_huawei_error_does_not_leak_sk():
    sk = "hw-secret-9f8e7d6c5b4a"
    cfg = {"ak": "ak", "sk": sk, "project_id": "pid", "regions": ["cn-north-4"]}
    with mock.patch.object(
        huawei, "_raw_instances", side_effect=RuntimeError(f"bad sk={sk}")
    ):
        try:
            huawei.fetch(cfg)
        except ProviderError as err:
            assert sk not in str(err), str(err)
            assert "华为云接口报错" in str(err), str(err)
        else:
            raise AssertionError("鉴权失败应当抛 ProviderError")
