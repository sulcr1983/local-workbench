"""测试执行器 —— 唯一允许得出「全绿」的入口.

退出码约定：
  0  ALL_GREEN           全部通过，且 SKIP = 0
  1  有失败
  3  有未登记的 SKIP
  4  一个用例都没收集到
  5  用例数低于基线（baseline.json 的 min_tests 只能升不能降）
  6  GREEN_WITH_SKIPS    通过但有登记过的 SKIP —— 不算全绿

用法：python tests/run_all.py [-v]
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
BASELINE = os.path.join(TESTS, "baseline.json")
ALLOWLIST = os.path.join(TESTS, "skip_allowlist.json")
REPORT = os.path.join(TESTS, "last_report.json")

sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)  # 让 test_*.py 能 import _sshd 这类辅助模块


class Skipped(Exception):
    """显式跳过；必须登记在 skip_allowlist.json 里，否则算失败."""


def skip(reason: str):
    raise Skipped(reason)


def discover() -> list[tuple[str, object]]:
    cases: list[tuple[str, object]] = []
    for name in sorted(os.listdir(TESTS)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        path = os.path.join(TESTS, name)
        spec = importlib.util.spec_from_file_location(name[:-3], path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        for fn_name, fn in inspect.getmembers(mod, inspect.isfunction):
            if fn_name.startswith("test_") and fn.__module__ == mod.__name__:
                cases.append((f"{name[:-3]}::{fn_name}", fn))
    return cases


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def main() -> int:
    verbose = "-v" in sys.argv
    baseline = load_json(BASELINE, {"min_tests": 0})
    allow = set(load_json(ALLOWLIST, []))
    min_tests = int(baseline.get("min_tests", 0))

    t0 = time.time()
    cases = discover()
    total = len(cases)

    if total == 0:
        print("[!] 一个用例都没收集到 —— 不可能是全绿")
        return 4

    passed, failed, skipped, blocked = 0, [], [], []
    skips: list[dict] = []
    failures: list[dict] = []

    for name, fn in cases:
        try:
            fn()
        except Skipped as err:
            if name in allow:
                skipped.append(name)
                skips.append({"case": name, "reason": str(err)})
                print(f"  SKIP  {name}  ({err})")
            else:
                blocked.append(name)
                print(f"  BLOCK {name}  (未登记跳过：{err})")
        except Exception as err:  # noqa: BLE001 - 测试执行器就该兜住一切
            failed.append(name)
            failures.append({"case": name, "error": f"{type(err).__name__}: {err}", "tb": traceback.format_exc()})
            print(f"  FAIL  {name}")
            print(f"        {type(err).__name__}: {err}")
            if verbose:
                print(traceback.format_exc())
        else:
            passed += 1
            print(f"  ok    {name}")

    duration = round(time.time() - t0, 2)

    if total < min_tests:
        print(f"\n[!] 用例数 {total} 低于基线 {min_tests} —— 有测试被删掉了")
        result, code = "BELOW_BASELINE", 5
    elif failed:
        result, code = "FAILED", 1
    elif blocked:
        result, code = "BLOCKED_SKIPS", 3
    elif skipped:
        result, code = "GREEN_WITH_SKIPS", 6
    else:
        result, code = "ALL_GREEN", 0

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "duration_s": duration,
        "total": total,
        "pass": passed,
        "fail": len(failed),
        "skip": len(skipped),
        "blocked": len(blocked),
        "min_tests": min_tests,
        "skips": skips,
        "failures": failures,
        "result": result,
        "exit_code": code,
    }
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print(f"  合计 {total}  通过 {passed}  失败 {len(failed)}  跳过 {len(skipped)}  未登记 {len(blocked)}")
    print(f"  耗时 {duration}s   →  {result}  (exit {code})")
    if result == "GREEN_WITH_SKIPS":
        print("  注意：有跳过就不叫全绿，别对外说「全部通过」。")
    return code


if __name__ == "__main__":
    sys.exit(main())
