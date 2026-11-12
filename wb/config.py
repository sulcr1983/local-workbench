"""路径与配置.

- 路径常量集中在这里，别在各模块里拼字符串。
- config.json 只是**交换格式**（导入导出用），真源是 SQLite（见 TECH_SPEC §5.2）。
"""

from __future__ import annotations

import os
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

# WB_DB 指定别的库：开第二个实例做对比、或者试空态时用，不影响正在跑的那份
DB_PATH = os.environ.get("WB_DB") or os.path.join(DATA_DIR, "workbench.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "workbench.log")
KNOWN_HOSTS = os.path.join(DATA_DIR, "wb_known_hosts")

# 导出时一律抹掉的字段名（凭据只留本机）
SECRET_KEYS = ("secret", "passphrase", "smtp_pass", "webhook_secret", "access_key_secret", "sk")
MASK = ""

# 界面默认开关（T16 设置页可改，落到 ui_cfg 表）
DEFAULT_UI: dict[str, Any] = {
    "show_overview": False,   # 库存总览默认藏起来，菜单只留必要的几项
    # 域名 / 证书 / 服务器到期 **必须默认可见**。
    # 原来这里默认 False，结果「到期提醒」整个入口在侧边栏消失，
    # 用户想管域名和 SSL 时找不到地方 —— 功能在，但等于没有。
    "show_expiries": True,
    "browser_alive": False,
}

DEFAULT_NOTIFY: dict[str, Any] = {
    "smtp_host": "",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_tls": True,
    "mail_to": "",
    "webhook_type": "dingtalk",   # dingtalk | wecom | custom
    "webhook_url": "",
    "webhook_secret": "",
    "ding_speak_ok": False,
}


def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def is_network_path(path: str) -> bool:
    """WAL 需要 -shm 内存映射，网络路径（UNC）上必坏，启动前必须先挡住."""
    p = os.path.abspath(path)
    if p.startswith("\\\\") or p.startswith("//"):
        return True
    return False


def mask(value: Any) -> Any:
    """给导出用：有值 → 空串（不是打星号，避免被误当真实值再导回去）."""
    return MASK if value else value


def masked_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """递归脱敏，键名命中 SECRET_KEYS 就抹掉."""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            out[k] = masked_config(v)
        elif isinstance(v, list):
            out[k] = [masked_config(i) if isinstance(i, dict) else i for i in v]
        elif k in SECRET_KEYS:
            out[k] = mask(v)
        else:
            out[k] = v
    return out


# URL 里携带凭据的参数名。webhook_url 这类字段不在 SECRET_KEYS 里，
# 但值里藏着 key / access_token —— 导出时必须单独处理，否则等于裸奔。
def mask_url_secrets(url: str) -> str:
    """把 URL 查询串里的 token / key 值掩掉，保留占位符让用户知道"这里原来有值"."""
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl in ("key", "access_token", "token", "secret", "signature", "sign"):
            kept.append((k, f"{v[:4]}***" if v else ""))
        else:
            kept.append((k, v))
    out = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    return out


def deep_mask_urls(obj: Any) -> Any:
    """递归遍历：任何字符串里像 URL 的都做凭据掩码，其余透传."""
    if isinstance(obj, dict):
        return {k: deep_mask_urls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_mask_urls(i) for i in obj]
    if isinstance(obj, str) and "://" in obj:
        return mask_url_secrets(obj)
    return obj


def setup_logging() -> None:
    """日志写 data/workbench.log；不往控制台吐 traceback（T8 验收要求）."""
    import logging

    ensure_dirs()
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # paramiko 的 transport 线程异常改写到文件，不再污染 stderr
    plog = logging.getLogger("paramiko.transport")
    plog.addHandler(handler)
    plog.propagate = False
