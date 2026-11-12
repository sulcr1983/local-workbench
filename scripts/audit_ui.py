"""全站 UI 审计：逐页 × 逐视口采集证据。"""
from __future__ import annotations
import json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from shot_ui import Browser

BASE = "http://127.0.0.1:8787"
OUT = os.path.join(ROOT, "data/_audit")
SHOTS = os.path.join(ROOT, "docs/assets/screenshots")

VIEWPORTS = [(1440, 900, "desktop"), (1024, 768, "tablet"), (760, 900, "narrow")]
PAGES = [
    ("/projects", "projects", "项目档案"),
    ("/servers", "servers", "服务器列表"),
    ("/expiries", "expiries", "到期提醒"),
    ("/engine", "engine", "分类引擎"),
    ("/settings", "settings", "设置"),
    ("/audit", "audit", "操作记录"),
    ("/welcome", "welcome", "新手向导"),
]

PROBE_JS = """(() => {
  const cs = (e) => getComputedStyle(e);
  const sizes = {};
  document.querySelectorAll('body *').forEach((e) => {
    if (!e.textContent || !e.textContent.trim()) return;
    sizes[cs(e).fontSize] = (sizes[cs(e).fontSize] || 0) + 1;
  });
  const interactive = [];
  document.querySelectorAll('a,button,input,select,textarea,[data-mbx-open],[data-reconnect],[data-reconnect-card],[data-bulk],[data-ui-toggle]').forEach((e) => {
    const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    interactive.push({
      tag: e.tagName.toLowerCase(), type: e.getAttribute('type') || '',
      text: (e.innerText || e.value || e.getAttribute('title') || '').trim().slice(0, 40),
      cls: String(e.className || '').slice(0, 48), href: e.getAttribute('href') || '',
      data: [...e.attributes].filter(a => a.name.startsWith('data-')).map(a => a.name),
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      disabled: !!e.disabled
    });
  });
  const cards = [...document.querySelectorAll('.wb-card')].map((e) => {
    const r = e.getBoundingClientRect();
    return { x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height) };
  });
  const main = document.querySelector('.wb-wrap') || document.querySelector('main');
  const mr = main ? main.getBoundingClientRect() : null;
  const overflowEls = [];
  document.querySelectorAll('body *').forEach((e) => {
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.right > innerWidth + 2) {
      overflowEls.push({ tag: e.tagName.toLowerCase(), cls: String(e.className || '').slice(0, 40),
                         right: Math.round(r.right), w: Math.round(r.width) });
    }
  });
  return JSON.stringify({
    url: location.pathname, title: document.title,
    bodyW: document.body.scrollWidth, viewW: innerWidth, pageH: document.body.scrollHeight,
    mainX: mr ? Math.round(mr.x) : null,
    fontSizes: Object.keys(sizes).sort((a, b) => parseFloat(a) - parseFloat(b)),
    fontCount: sizes, interactive: interactive, cards: cards,
    overflowEls: overflowEls.slice(0, 8),
    textHead: document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 400)
  });
})()"""

def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    report = {"pages": [], "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    b = Browser()
    try:
        b.start(BASE + "/projects")
        for path, slug, label in PAGES:
            for w, h, vname in VIEWPORTS:
                b._cmd("Emulation.setDeviceMetricsOverride", width=w, height=h,
                       deviceScaleFactor=1, mobile=False)
                b.goto(BASE + path)
                time.sleep(0.45)
                try:
                    raw = b._cmd("Runtime.evaluate", expression=PROBE_JS, returnByValue=True)
                    data = json.loads(raw["result"]["value"])
                except Exception as err:
                    data = {"url": path, "error": str(err)[:200]}
                data.update({"slug": slug, "label": label, "viewport": vname, "vp": f"{w}x{h}"})
                report["pages"].append(data)
                if vname == "desktop":
                    b.shot(f"{SHOTS}/audit-{slug}.png", full=True)
                elif vname == "narrow":
                    b.shot(f"{SHOTS}/audit-{slug}-760.png")
            print(f"  {path:14s} 采完")
    finally:
        b.close()
    with open(f"{OUT}/report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("\n=== 溢出 / 字号体检 ===")
    for p in report["pages"]:
        if "error" in p:
            print(f"  {p.get('slug')} {p.get('vp')} 失败：{p['error'][:80]}")
            continue
        flag = "" if p["bodyW"] <= p["viewW"] else f"  << 越界 bodyW={p['bodyW']}"
        print(f"  {p['label']:8s} {p['vp']:9s} 字号 {','.join(p['fontSizes'])}{flag}")
    print(f"\n报告：{OUT}/report.json")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
