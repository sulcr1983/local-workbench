"""启动器（start.bat）与环境自检的门禁测试。

这两类问题都属于「静默失效」，页面照常能开所以不会有人发现：
  1. start.bat 编码/行尾不对 —— cmd 按字节偏移解析批处理，
     UTF-8 + LF 会让中文行之后的命令被截断成乱码报错，或者干脆不执行。
  2. start.bat 的依赖探测清单漏包 —— 每次双击都进 pip 全量重装，
     看起来像「卡住」，其实就是白等几分钟。
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "start.bat"


def _bat_text() -> str:
    """按 GBK 读；这就是 cmd 在中文 Windows 上的实际读法."""
    return BAT.read_bytes().decode("gbk")


# ---------------------------------------------------------------- 文件编码


def test_start_bat_is_gbk_with_crlf():
    """必须是 GBK + CRLF。UTF-8 或 LF 都会让 cmd 解析错位."""
    raw = BAT.read_bytes()

    # 有中文才谈得上错位；纯 ASCII 的批处理编码无所谓，但那份提示也就没了
    assert any(b > 0x7F for b in raw), "start.bat 里的中文提示不该被删光"

    try:
        raw.decode("gbk")
    except UnicodeDecodeError as err:
        raise AssertionError(
            f"start.bat 不是 GBK 编码，cmd 会读成乱码：{err}"
        ) from None

    # 反向确认：真 GBK 中文基本不可能同时是合法 UTF-8
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("start.bat 看起来是 UTF-8，必须存成 GBK(cp936)")

    assert raw.count(b"\n") == raw.count(b"\r\n"), "存在裸 LF 行，cmd 会解析异常"


def test_start_bat_does_not_switch_codepage():
    """chcp 65001 + 中文批处理 = 偏移错位的经典组合，别再引入."""
    text = _bat_text()
    assert "65001" not in text, "不要在 start.bat 里 chcp 65001，用 GBK 就好"


# ---------------------------------------------------------------- 依赖探测


def test_start_bat_dep_probe_covers_importable_packages():
    """探测清单里的包必须真的装得上，否则每次启动都白跑一遍 pip."""
    text = _bat_text()
    match = re.search(r'-c\s+"import ([^"]+)"', text)
    assert match, "没找到依赖探测那一行（import ...），bat 结构变了要同步改这个测试"

    modules = [m.strip() for m in match.group(1).split(",") if m.strip()]
    assert "flask" in modules and "waitress" in modules, modules

    missing = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    assert not missing, f"这些包探测得到但导入失败，启动会退化成每次重装：{missing}"


def test_start_bat_does_not_probe_lazy_cloud_sdks():
    """两家云 SDK 是函数内延迟导入，不该出现在探测清单里（缺了也不该拦住启动）."""
    match = re.search(r'-c\s+"import ([^"]+)"', _bat_text())
    modules = [m.strip() for m in match.group(1).split(",")]
    for lazy in ("alibabacloud_ecs20140526", "huaweicloudsdkecs"):
        assert lazy not in modules, f"{lazy} 是延迟导入，探测它会误判依赖缺失"


# ---------------------------------------------------------------- 调度器包装


def test_scheduler_wrap_records_both_success_and_failure():
    """_wrap 要落运行记录，且单个 job 抛异常不能把调度器带崩."""
    from wb import scheduler

    calls: list[tuple] = []
    original = scheduler._record
    scheduler._record = lambda jid, ok, err="": calls.append((jid, ok, err))
    try:
        scheduler._wrap("probe-ok", lambda: {"ok": 1})()

        def boom():
            raise RuntimeError("boom")

        scheduler._wrap("probe-bad", boom)()
    finally:
        scheduler._record = original

    assert calls == [("probe-ok", True, ""), ("probe-bad", False, "boom")], calls
