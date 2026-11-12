"""批量有头截图 + 几何体检 + 结构回归基线（T17 验收用）。

检查项：
- 每页 body 宽度不超过视口（没有横向滚动）
- 主内容容器 x >= 侧栏宽度（没有被挤出布局）
- 记录可见字号种类（只能落在 6 档令牌里）
- 与上一次记录的指纹比对（`--record` 写基线）：布局骨架、字号档位、主色集合

为什么不做像素 diff：本机 DPI 150%，字体栅格化跨会话会有 1px 级噪声，
像素法天天误报。结构指纹只盯「布局骨架 + 设计令牌 + 语义色」，
这三样任何一样被改坏都是真回归，且不受数据量变化影响。
"""

import argparse
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from shot_ui import Browser  # noqa: E402

OUT = r"D:\test\workbench\docs\assets\screenshots"
BASE = r"D:\test\workbench\data\_audit\ui_baseline.json"
HOST = "http://127.0.0.1:8787"
BASE_URL = HOST

PAGES = [
    ("/projects", "v2-projects.png", True),
    ("/project/demo-collector", "v2-project-detail.png", False),
    ("/servers", "v2-servers.png", True),
    ("/expiries", "v2-expiries.png", True),
    ("/engine", "v2-engine.png", True),
    ("/settings", "v2-settings.png", True),
    ("/audit", "v2-audit.png", True),
]

GEOM = r"""
(() => {
  const r = (s) => {
    const e = document.querySelector(s);
    if (!e) return null;
    const b = e.getBoundingClientRect();
    return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height)};
  };
  const sizes = {};
  const bg = {};
  document.querySelectorAll('body *').forEach((e) => {
    const st = getComputedStyle(e);
    const c = st.backgroundColor;
    if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') bg[c] = (bg[c] || 0) + 1;
    if (!e.textContent || !e.textContent.trim()) return;
    sizes[st.fontSize] = (sizes[st.fontSize] || 0) + 1;
  });
  // 只留「经常出现」的背景色，长尾（只出现一两次的）会随数据抖动，不能当基线
  const bgTop = Object.keys(bg).filter((k) => bg[k] >= 3).sort().slice(0, 10);
  return JSON.stringify({
    path: location.pathname,
    title: document.title,
    rail: r('.wb-rail'),
    main: r('.wb-main'),
    wrap: r('.wb-wrap'),
    bodyW: document.body.scrollWidth,
    viewW: innerWidth,
    bodyH: document.body.scrollHeight,
    fontSizes: sizes,
    bgTop: bgTop,
    nCard: document.querySelectorAll('.wb-card').length,
  });
})()
"""


def first_server_detail() -> str:
    """挑一台服务器详情页。没有服务器就退回列表页，不让基线脚本因为空库罢工."""
    try:
        with urllib.request.urlopen(HOST + "/servers", timeout=5) as r:
            html = r.read().decode("utf-8", "ignore")
        m = re.search(r'/server/(\d+)', html)
        if m:
            return f"/server/{m.group(1)}"
    except Exception:  # noqa: BLE001 - 纯展示用途，取不到就不测这一页
        pass
    return "/servers"


def fingerprint(info: dict) -> dict:
    """只留「布局骨架 + 设计令牌 + 语义色」，不掺数据量."""
    main = info.get("main") or {}
    wrap = info.get("wrap") or {}
    rail = info.get("rail") or {}
    return {
        "main_x": main.get("x"),
        "main_w": main.get("w"),
        "wrap_w": wrap.get("w"),
        "rail_w": rail.get("w"),
        "fontSizes": sorted(info.get("fontSizes") or {}),
        "bgTop": sorted(info.get("bgTop") or []),
    }


def diff_fp(path: str, old: dict, new: dict) -> tuple[list[str], list[str]]:
    """返回 (硬错, 提示)。硬错 = 布局塌 / 令牌外字号；提示 = 宽度漂移 / 配色增减."""
    errs, warns = [], []
    if old.get("main_x") is None or new.get("main_x") is None:
        errs.append(f"{path}: 主内容容器没了（之前 {old.get('main_x')} 之后 {new.get('main_x')}）")
        return errs, warns
    if abs(old["main_x"] - new["main_x"]) > 2:
        errs.append(f"{path}: 主内容 x 从 {old['main_x']} 变成 {new['main_x']}（布局被挤出）")
    for k in ("main_w", "wrap_w", "rail_w"):
        o, n = old.get(k), new.get(k)
        if o and n and abs(o - n) > max(2, o * 0.02):
            warns.append(f"{path}: {k} {o} → {n}")
    if old["fontSizes"] != new["fontSizes"]:
        added = set(new["fontSizes"]) - set(old["fontSizes"])
        gone = set(old["fontSizes"]) - set(new["fontSizes"])
        if added:
            errs.append(f"{path}: 出现新字号 {sorted(added)}（应在 6 档令牌内）")
        if gone:
            warns.append(f"{path}: 少了字号 {sorted(gone)}")
    added_c = set(new["bgTop"]) - set(old["bgTop"])
    gone_c = set(old["bgTop"]) - set(new["bgTop"])
    if added_c or gone_c:
        warns.append(f"{path}: 主色集合变化 +{len(added_c)} -{len(gone_c)}")
    return errs, warns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="把当前状态记为基线")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    pages = PAGES + [(first_server_detail(), "v2-server-detail.png", False)]

    b = Browser()
    problems: list[str] = []
    report = []
    try:
        b.start(BASE_URL + "/projects")
        for path, name, full in pages:
            b.goto(BASE_URL + path)
            info = json.loads(
                b._cmd("Runtime.evaluate", expression=GEOM, returnByValue=True)["result"]["value"]
            )
            b.shot(os.path.join(OUT, name), full=full)
            report.append(info)

            if info["bodyW"] > info["viewW"] + 1:
                problems.append(f"{path}: 横向溢出 bodyW={info['bodyW']} viewW={info['viewW']}")
            if not info["main"] or info["main"]["x"] < 200:
                problems.append(f"{path}: 主内容 x={info['main'] and info['main']['x']}（应 >= 200）")
            if not info["wrap"]:
                problems.append(f"{path}: 找不到 .wb-wrap")
            allowed = {"12.5px", "14px", "17px", "20px", "24px", "32px"}
            extra = {k: v for k, v in info["fontSizes"].items() if k not in allowed}
            if extra:
                problems.append(f"{path}: 出现了令牌外字号 {extra}")
            print(f"{path:26s} bodyW={info['bodyW']:5d} viewW={info['viewW']} "
                  f"main.x={info['main']['x'] if info['main'] else '-'} h={info['bodyH']}")
    finally:
        b.close()

    # ---------------------------------------------- 结构回归比对
    current = {i["path"]: fingerprint(i) for i in report}
    warns: list[str] = []
    if args.record:
        os.makedirs(os.path.dirname(BASE), exist_ok=True)
        with open(BASE, "w", encoding="utf-8") as f:
            json.dump({"recorded_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                       "pages": current}, f, ensure_ascii=False, indent=1)
        print(f"\n已记录基线：{BASE}（{len(current)} 页）")
    elif os.path.isfile(BASE):
        with open(BASE, encoding="utf-8") as f:
            old = json.load(f).get("pages") or {}
        for path, fp in current.items():
            if path not in old:
                warns.append(f"{path}: 基线里没有这页（新增页面，建议 --record 更新）")
                continue
            e, w = diff_fp(path, old[path], fp)
            problems.extend(e)
            warns.extend(w)
    else:
        print(f"\n没有基线文件（{BASE}）；本次只做内建断言。加 --record 可建立基线。")

    print()
    if warns:
        print("WARN:")
        for w in warns:
            print("  ~", w)
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"ALL_OK  {len(report)} 页")
    return 0


if __name__ == "__main__":
    sys.exit(main())
