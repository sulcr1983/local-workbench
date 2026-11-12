"""逐页核对：拿原型的结构特征和线上页面做机器比对。

比的是「有什么」而不是「长什么样」：
页面 h1、副标题、筛选 chip、按钮、表头、卡片标题 —— 这些缺了用户一眼就看出来。
"""

import io
import re
import sys
import urllib.request

PROTO = r"D:\test\workbench\prototype\主原型-工作台v7.html"
BASE = "http://127.0.0.1:8787"

PAGES = [
    ("overview", "/", "库存总览"),
    ("projects", "/projects", "项目档案"),
    ("servers", "/servers", "服务器和云资源"),
    ("engine", "/engine", "分类引擎"),
    ("audit", "/audit", "操作记录"),
    ("settings", "/settings", "设置"),
]


def strip(html: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", txt).strip()


def feat(html: str) -> dict:
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    return {
        "h1": strip(h1.group(1)) if h1 else "",
        "th": [strip(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)],
        "chips": [strip(x) for x in re.findall(r'class="[^"]*\bchip\b[^"]*"[^>]*>(.*?)</a>', html, re.S)],
        "btns": [strip(x) for x in re.findall(r'class="[^"]*\bbtn\b[^"]*"[^>]*>(.*?)</(?:button|a)>', html, re.S)],
        "cards": [strip(x) for x in re.findall(r'<div class="ch"[^>]*>.*?<h2[^>]*>(.*?)</h2>', html, re.S)],
        "rows": html.count("<tr>") + html.count("<tr "),
    }


def fetch(url: str) -> str:
    with urllib.request.urlopen(BASE + url, timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def main() -> int:
    proto = io.open(PROTO, encoding="utf-8").read()
    total_gap = 0

    for view, url, label in PAGES:
        m = re.search(r'<section data-view="%s"(.*?)</section>' % re.escape(view), proto, re.S)
        if not m:
            print(f"[{label}] 原型里没有这个视图，跳过")
            continue
        p = feat(m.group(1))
        try:
            o = feat(fetch(url))
        except Exception as err:  # noqa: BLE001
            print(f"[{label}] 抓取失败：{err}")
            continue

        print(f"\n=== {label}  {url} ===")
        print(f"  h1      原型「{p['h1']}」 / 我们「{o['h1']}」")

        miss_th = [x for x in p["th"] if x and x not in o["th"]]
        extra_th = [x for x in o["th"] if x and x not in p["th"]]
        if miss_th:
            print(f"  表头缺：{miss_th}")
            total_gap += len(miss_th)
        if extra_th:
            print(f"  表头多：{extra_th}")

        # chip 文案在原型里可能是「活跃 3」这种带数字的，取前缀比
        p_chips = [re.sub(r"\d+$", "", c).strip() for c in p["chips"] if c]
        o_chips = [re.sub(r"\d+$", "", c).strip() for c in o["chips"] if c]
        miss_chip = [c for c in p_chips if c and c not in o_chips]
        if miss_chip:
            print(f"  筛选缺：{miss_chip}")
            total_gap += len(miss_chip)

        p_btns = [b for b in p["btns"] if b]
        o_btns = [b for b in o["btns"] if b]
        miss_btn = [b for b in p_btns if b not in o_btns]
        if miss_btn:
            print(f"  按钮缺：{miss_btn[:8]}")
            total_gap += len(miss_btn)

        p_cards = [c for c in p["cards"] if c]
        o_cards = [c for c in o["cards"] if c]
        miss_card = [c for c in p_cards if c not in o_cards]
        if miss_card:
            print(f"  卡片缺：{miss_card[:8]}")
            total_gap += len(miss_card)

    print(f"\n合计缺口 {total_gap} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
