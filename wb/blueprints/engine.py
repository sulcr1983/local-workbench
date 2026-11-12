"""自动分类与判断引擎页：把「什么算项目」摊开给人看，也让人能改.

判定口径：按 GitHub 上真实仓库根的特征**打分**（.git / 依赖清单 / README /
LICENSE / .gitignore / CI / Docker / 构建入口），攒够 5 分才算项目。
扫描时就把目录分三档，落库在 project.state：

    auto       ≥5 分 —— 自动收编，进项目列表
    candidate  3–4 分 —— 像项目但证据不够硬，等人定
    dropped    <3 分，或 _ 开头 / 容器名 / 应用数据目录 —— 引擎减掉

页面只做两件事：把减法过程说清楚（可解释），把候选交给人决定（可操作）。
"""

from __future__ import annotations

import os

from flask import Blueprint, render_template, request, url_for

from .. import db
from ..paging import paginate
from ..scan import SCORE_AUTO, SCORE_CANDIDATE, looks_like_app_data, root_score

bp = Blueprint("engine", __name__)

PER_PAGE = 15

# 三档的文案；模板和计数共用一份，避免两处走样
STATES = {
    "auto": ("已收编", "攒够 5 分，自动进项目列表"),
    "candidate": ("候选", "3–4 分，像项目但证据不够硬 —— 要不要算你说了算"),
    "dropped": ("已剔除", "不到 3 分，或是 _ 开头 / 容器目录 —— 引擎直接减掉"),
}

SRC_LABEL = {
    "readme_h1": "README 标题",
    "manifest": "清单文件",
    "dirname": "目录名",
}

# 打分表（参考 GitHub 上真实仓库根的常见构成：版本控制 + 说明 + 许可 + 依赖声明 + CI/构建）
# 单项都不足以说明「这是项目根」，攒够分才算。
# 分值用整数，模板要拿它算点阵强度（+5 点亮 5 格）。
RULES = [
    (".git/", 5, "版本控制根 —— 有它就是个仓库"),
    ("依赖清单：pyproject.toml / package.json / go.mod / Cargo.toml …（共 9 种）",
     4, "能装能跑的工程才有的声明文件"),
    ("README.md / README.txt", 3, "项目门面，也是取名来源"),
    ("LICENSE / COPYING", 2, "开源项目几乎标配，也说明「这东西有主」"),
    (".gitignore / .gitattributes", 2, "只在项目根有意义，子目录一般不放"),
    (".github/ 或 .gitlab-ci.yml", 2, "CI 配置只在根目录"),
    ("Dockerfile / docker-compose / compose.yaml", 2, "容器化部署入口"),
    ("Makefile / justfile / Taskfile", 1, "构建入口"),
    ("CONTRIBUTING / CHANGELOG / SECURITY 等", 1, "协作与工程化文件"),
    ("根目录直接放着 ≥3 个源码文件", 1, "纯文档目录一般没有"),
]

# 按权重分三档展示：人扫一眼就知道哪几条是硬判据、哪几条只是帮忙抬分
RULE_GROUPS = [
    ("决定性证据", "命中任意一条，基本就锁定是项目根", [r for r in RULES if r[1] >= 4]),
    ("辅助证据", "单独不够硬，得配上前面的线索才成立", [r for r in RULES if 2 <= r[1] <= 3]),
    ("弱信号", "只在别的证据都缺时帮忙抬一手", [r for r in RULES if r[1] == 1]),
]


def _rows() -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM project ORDER BY state, path"):
        out.append(
            {
                "path": r["path"],
                "name": r["user_name"] or r["name_auto"] or r["dirname"],
                "dirname": r["dirname"],
                "name_src": r["name_src"] or "dirname",
                "name_conf": r["name_conf"],
                "manual": bool(r["user_name"]),
                "stack": r["stack"] or "未识别",
                "active": r["active"] or "未知",
                "is_git": bool(r["is_git"]),
                "state": (r["state"] if "state" in r.keys() else "auto") or "auto",
                "locked": bool(r["state_locked"]) if "state_locked" in r.keys() else False,
                "last_seen": r["last_seen"],
                **_score_of(r["path"]),
            }
        )
    return out


def _score_of(path: str) -> dict:
    """现算一次分数。60 个目录的 listdir 加起来不到 50ms，不用缓存."""
    try:
        names = set(os.listdir(path))
    except OSError:
        return {"score": 0, "hits": [], "gone": True, "app_data": False}
    score, hits = root_score(names)
    return {"score": score, "hits": hits, "gone": False, "app_data": looks_like_app_data(names)}


def _why(item: dict) -> str:
    """这一条为什么被判成这样 —— 把命中的分项列出来，人能自己复核."""
    if item["gone"]:
        return "目录已经不在了"
    if item["app_data"]:
        return "浏览器/应用数据目录（里面有 Chrome 自动生成的 README）"
    if item["locked"]:
        return "你手动改过判定，重扫不会覆盖"
    if item["state"] == "candidate":
        return f"{item['score']} 分，差 {SCORE_AUTO - item['score']} 分到自动收编 —— 你说了算"
    if item["state"] == "dropped":
        return f"只有 {item['score']} 分，够不上「项目根」"
    return " + ".join(item["hits"]) if item["hits"] else "有 README，自动收编"


@bp.get("/engine")
def index():
    items = _rows()
    stage = request.args.get("stage") or "candidate"
    if stage not in ("auto", "candidate", "dropped", "all"):
        stage = "candidate"

    counts = {
        "all": len(items),
        "auto": sum(1 for i in items if i["state"] == "auto"),
        "candidate": sum(1 for i in items if i["state"] == "candidate"),
        "dropped": sum(1 for i in items if i["state"] == "dropped"),
    }

    # 漏斗四段：看过 → 收编 → 候选 → 剔除
    stages = [
        ("all", "看过的目录", counts["all"]),
        ("auto", "自动收编", counts["auto"]),
        ("candidate", "待你定", counts["candidate"]),
        ("dropped", "引擎剔除", counts["dropped"]),
    ]

    shown = items if stage == "all" else [i for i in items if i["state"] == stage]
    chunk, page = paginate(shown, request.args.get("page"), PER_PAGE)

    def mk_url(n: int) -> str:
        return url_for("engine.index", stage=stage, page=n)

    roots = db.query("SELECT path, added_at FROM root ORDER BY added_at")
    last_scan = db.scalar("SELECT MAX(last_seen) FROM project") or ""

    return render_template(
        "engine.html",
        page_title="分类引擎",
        page_code="ENGINE / 005",
        crumb="分类引擎",
        active="engine",
        stages=stages,
        counts=counts,
        stage=stage,
        states=STATES,
        rules=RULES,
        rule_groups=RULE_GROUPS,
        score_auto=SCORE_AUTO,
        score_cand=SCORE_CANDIDATE,
        rule_total=len(RULES),
        src_label=SRC_LABEL,
        items=chunk,
        why={i["path"]: _why(i) for i in chunk},
        page=page,
        total_all=len(shown),
        mk_url=mk_url,
        roots=roots,
        last_scan=last_scan,
        counts_nav={},
    )
