"""项目详情页的可视化计算.

原则：**不编数据**。每个数字都从库里 / git 里真读出来，
图形只是把已有信息换个更容易扫读的形状。取不到就明说取不到。

三个纯函数，全部可单测：
- `commit_kind()` 提交标题 → 类型（feat / fix / docs …）+ 图标 + 颜色
- `week_buckets()` 提交 → 最近 N 周的柱状分布（看「还在不在动」）
- `health()`    项目 → 四维健康分（活跃 / 同步 / 文档 / 部署）
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

# --------------------------------------------------------------- 提交类型
# 约定俗成的前缀（Conventional Commits + 中文团队常见写法）。
# 认不出就归 other —— 不硬猜，猜错比不猜更烦人。
_COMMIT_KINDS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("feat", ("feat", "feature", "新增", "添加", "增加"), "新增功能", "#2E7D32"),
    ("fix", ("fix", "bugfix", "hotfix", "修复", "修正", "修补"), "修问题", "#A32D2D"),
    ("docs", ("docs", "doc", "文档", "注释"), "文档", "#185FA5"),
    ("test", ("test", "tests", "测试"), "测试", "#6B3FA0"),
    ("refactor", ("refactor", "重构"), "重构", "#0E6E86"),
    ("perf", ("perf", "性能", "优化"), "性能", "#B26A00"),
    ("style", ("style", "format", "格式"), "格式", "#6E7681"),
    ("chore", ("chore", "build", "ci", "deps", "杂务", "依赖", "构建"), "杂务", "#6E7681"),
    ("ops", ("ops", "deploy", "运维", "部署", "发布"), "运维", "#8A5200"),
    ("revert", ("revert", "回滚", "撤销"), "回滚", "#A32D2D"),
)

# 图标名要跟 templates/_parts/ui.html 的 icon 宏对得上
_KIND_ICON = {
    "feat": "sparkle",
    "fix": "bug",
    "docs": "book",
    "test": "check",
    "refactor": "refresh",
    "perf": "activity",
    "style": "edit",
    "chore": "gear",
    "ops": "rocket",
    "revert": "back",
    "other": "commit",
}


def commit_kind(subject: str) -> dict:
    """提交标题 → {kind, label, icon, color}.

    认 `fix(xxx): ...` / `Fix: ...` / `修复xxx` / `[feat] xxx` 这几种写法。
    """
    s = (subject or "").strip()
    if not s:
        return {"kind": "other", "label": "其他", "icon": _KIND_ICON["other"], "color": "#8A909B"}

    head = s.lstrip("[（(【").lower()
    # 取到第一个分隔符之前的部分当"前缀区"
    prefix = re.split(r"[:：\]）)】\s]", head, maxsplit=1)[0]

    for kind, words, label, color in _COMMIT_KINDS:
        for w in words:
            if prefix == w or prefix.startswith(w):
                return {"kind": kind, "label": label, "icon": _KIND_ICON[kind], "color": color}

    # 没有前缀：看标题里有没有明显的动作词（中文团队常这么写）
    for kind, words, label, color in _COMMIT_KINDS:
        if kind in ("chore", "style"):
            continue
        for w in words:
            if w in s:
                return {"kind": kind, "label": label, "icon": _KIND_ICON[kind], "color": color}

    return {"kind": "other", "label": "其他", "icon": _KIND_ICON["other"], "color": "#8A909B"}


def _parse_day(text: str) -> date | None:
    try:
        return datetime.strptime((text or "")[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def week_buckets(commits: list[dict], weeks: int = 8, today: date | None = None) -> list[dict]:
    """最近 N 周的提交数，最早的在前.

    用来回答一个具体的疑问：「这项目最近还在动吗？」
    比「最后提交 2026-09-12」这种单点更直观 —— 单点看不出是持续在写还是三个月前猛写一阵。

    **只在有数据的跨度内画**：如果最近 8 周只有最后 2 周有提交，就只画这 2 周，
    而不是用 6 个空格位占满屏 —— 那 6 个空槽不携带任何信息，只是把图撑开。
    """
    today = today or date.today()
    # 对齐到本周一，保证桶边界稳定（不会因为今天星期几而漂）
    this_monday = today - timedelta(days=today.weekday())
    buckets = []
    for i in range(weeks - 1, -1, -1):
        start = this_monday - timedelta(weeks=i)
        buckets.append({"start": start, "label": start.strftime("%m/%d"), "count": 0})

    for c in commits:
        d = _parse_day(c.get("date") or "")
        if d is None:
            continue
        idx = weeks - 1 - (this_monday - (d - timedelta(days=d.weekday()))).days // 7
        if 0 <= idx < weeks:
            buckets[idx]["count"] += 1

    # 砍掉前面连续的空周（保留至少 1 个，好让人看到"上上周也没动"）
    first_hit = next((i for i, b in enumerate(buckets) if b["count"]), None)
    if first_hit is not None and first_hit > 0:
        buckets = buckets[max(0, first_hit - 1):]

    peak = max((b["count"] for b in buckets), default=0)
    for b in buckets:
        # 高度百分比：至少 6%，否则「有 1 次」会看不出来
        b["pct"] = int(round(b["count"] / peak * 100)) if peak else 0
        b["pct"] = max(b["pct"], 6) if b["count"] else 0
    return buckets


# --------------------------------------------------------------- 健康度
def health(p: dict, has_readme: bool, linked_n: int, today: date | None = None) -> dict:
    """四维健康分。每项 0-100，总分取平均。

    这四项对应「一个项目还活着吗」的四个证据：
    最近动过 / 改动有归宿 / 别人看得懂 / 知道跑在哪。
    缺哪项就扣哪项的分，并在界面上直接说缺什么 —— 不是给个笼统的"良好"。
    """
    today = today or date.today()
    items: list[dict] = []

    # ---- 1. 活跃度（最后提交距今）
    last = _parse_day(p.get("last_commit") or "")
    if last is None:
        items.append({"key": "active", "label": "活跃度", "pct": 0,
                      "text": "没有提交记录", "icon": "clock", "color": "#A32D2D"})
    else:
        days = (today - last).days
        if days <= 7:
            pct, text, color = 100, f"{days} 天前动过", "#2E7D32"
        elif days <= 30:
            pct, text, color = 70, f"{days} 天前动过", "#2E7D32"
        elif days <= 90:
            pct, text, color = 40, f"{days} 天没动了", "#B26A00"
        else:
            pct, text, color = 10, f"{days} 天没动了", "#A32D2D"
        items.append({"key": "active", "label": "活跃度", "pct": pct,
                      "text": text, "icon": "clock", "color": color})

    # ---- 2. 同步（改动有没有归宿）
    if not p.get("is_git"):
        items.append({"key": "sync", "label": "版本管理", "pct": 0,
                      "text": "不是 git 仓库", "icon": "branch", "color": "#A32D2D"})
    else:
        dirty = int(p.get("dirty_n") or 0)
        unpushed = int(p.get("unpushed_n") or 0)
        if dirty == 0 and unpushed == 0:
            pct, text, color = 100, "全部已提交并推送", "#2E7D32"
        elif dirty and unpushed:
            pct, text, color = 30, f"{dirty} 个文件没提交、{unpushed} 个没推", "#A32D2D"
        elif unpushed:
            pct, text, color = 55, f"{unpushed} 个提交没推", "#B26A00"
        else:
            pct, text, color = 40, f"{dirty} 个文件没提交", "#B26A00"
        items.append({"key": "sync", "label": "版本管理", "pct": pct,
                      "text": text, "icon": "branch", "color": color})

    # ---- 3. 文档
    if has_readme:
        items.append({"key": "doc", "label": "文档", "pct": 100,
                      "text": "有 README", "icon": "book", "color": "#2E7D32"})
    else:
        items.append({"key": "doc", "label": "文档", "pct": 0,
                      "text": "没有 README", "icon": "book", "color": "#A32D2D"})

    # ---- 4. 部署位置
    if linked_n or p.get("run_local"):
        if p.get("run_local") and linked_n:
            text = f"本机 + {linked_n} 台服务器"
        elif p.get("run_local"):
            text = "跑在本机"
        else:
            text = f"{linked_n} 台服务器"
        items.append({"key": "deploy", "label": "部署位置", "pct": 100,
                      "text": text, "icon": "server", "color": "#2E7D32"})
    else:
        items.append({"key": "deploy", "label": "部署位置", "pct": 0,
                      "text": "没说跑在哪", "icon": "server", "color": "#6E7681"})

    score = int(round(sum(i["pct"] for i in items) / len(items)))
    if score >= 80:
        grade, gcolor = "健康", "#2E7D32"
    elif score >= 55:
        grade, gcolor = "还行", "#B26A00"
    elif score >= 30:
        grade, gcolor = "要关注", "#B26A00"
    else:
        grade, gcolor = "像废弃了", "#A32D2D"

    return {"score": score, "grade": grade, "color": gcolor, "parts": items}


# --------------------------------------------------------------- 技术栈
_STACK_ICON = {
    "Python": "py", "Node": "node", "Go": "go", "Java": "java",
    "静态页": "static", "未识别": "none",
}


def stack_key(stack: str) -> str:
    """技术栈名 → 图标/配色 key.

    detect_stack 现在会返回「Python（散装）」这种带后缀说明的值，
    这里去掉括号再匹配 —— 否则所有散装项目都会掉到 none 上。
    """
    s = (stack or "").strip()
    base = s.split("（")[0].split("(")[0].strip()
    return _STACK_ICON.get(base, "none")


# --------------------------------------------------------------- 项目体检
def risks(p: dict, *, has_readme: bool, linked_n: int,
          today: date | None = None) -> list[dict]:
    """把「这个项目有什么坑」列成清单，红黄绿分级.

    PM 视角不是"分数高不高"，而是"我现在要不要插手、插手先干什么"。
    所以每条都带一个**能立刻执行的动作**，而不是一句评价。
    """
    today = today or date.today()
    out: list[dict] = []

    dirty = int(p.get("dirty_n") or 0)
    unpushed = int(p.get("unpushed_n") or 0)
    last = _parse_day(p.get("last_commit") or "")
    days = (today - last).days if last else None

    # 红：会丢东西的
    if dirty >= 20:
        out.append({"lv": "err", "t": f"{dirty} 个文件没提交",
                    "d": "改动只在工作区，误删或换机就没了", "act": "去提交"})
    if unpushed >= 10:
        out.append({"lv": "err", "t": f"{unpushed} 个提交没推",
                    "d": "只在你本机，硬盘坏了就没了", "act": "去 push"})
    if p.get("is_git") and not (p.get("remote") or "").strip():
        out.append({"lv": "warn", "t": "没有远程仓库",
                    "d": "连个备份都没有，也没法协作", "act": "加 origin"})

    # 黄：会拖慢你的
    if not has_readme:
        out.append({"lv": "warn", "t": "没有 README",
                    "d": "别人接手要一个个文件猜它是干什么的", "act": "补 3 行说明"})
    if days is not None and days > 90:
        out.append({"lv": "warn", "t": f"{days} 天没动过",
                    "d": "大概率烂尾，或者已经不需要了", "act": "决定续还是归档"})

    # 绿：不是问题，但值得知道
    if not linked_n and not p.get("run_local"):
        out.append({"lv": "mut", "t": "没说跑在哪",
                    "d": "下次要改线上时会想不起来", "act": "标一下部署位置"})
    if not p.get("is_git"):
        out.append({"lv": "mut", "t": "不是 git 仓库",
                    "d": "没有版本历史，改坏了回不去", "act": "git init"})

    if not out:
        out.append({"lv": "ok", "t": "没发现明显问题",
                    "d": "提交、推送、文档、部署位置都对得上", "act": ""})
    return out


# --------------------------------------------------------------- 工作节奏
def cadence(commits: list[dict], today: date | None = None) -> dict:
    """从提交历史算出「这个项目是按什么节奏在推进」.

    全部是 git 里的硬事实，没有任何推测 —— 这是它可信的原因。
    回答 PM 真正会问的三个问题：
      1. 还在推进吗？（最后一次 / 两次提交间隔）
      2. 是冲刺式还是细水长流？（活跃天数占比）
      3. 一共干了多少？（总提交 / 跨度天数）

    `commits` 必须是**时间窗口**取来的完整集合（`git_commits_since`），
    不能是"最近 N 条"采样 —— 否则活跃期会被低估。
    """
    today = today or date.today()
    days: list[date] = []
    for c in commits:
        d = _parse_day(c.get("date") or "")
        if d:
            days.append(d)

    if not days:
        return {"empty": True, "total": 0}

    days.sort()
    first, last = days[0], days[-1]
    span = max((today - first).days, 1)
    active_days = len(set(days))
    idle = (today - last).days

    # 两次提交的中位间隔：比平均值抗极端值（一天 25 条的那种）
    uniq = sorted(set(days))
    gaps = [(uniq[i] - uniq[i - 1]).days for i in range(1, len(uniq))]
    if gaps:
        gaps.sort()
        mid = len(gaps) // 2
        median_gap = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    else:
        median_gap = 0

    # 冲刺 vs 细水长流：有提交的天数 / 跨度天数
    density = active_days / span * 100

    if idle <= 7 and density >= 30:
        mode, mode_hint = "持续在推", "最近一周有提交，且经常在动"
    elif idle <= 7:
        mode, mode_hint = "在做，但不密", "最近一周动过，但隔几天才提交一次"
    elif idle <= 30:
        mode, mode_hint = "缓下来了", f"{idle} 天没提交，可能在做别的"
    else:
        mode, mode_hint = "停了", f"{idle} 天没有任何提交"

    return {
        "empty": False,
        "total": len(days),
        "span_days": span,
        "active_days": active_days,
        "density": round(density, 1),
        "median_gap": median_gap,
        "idle_days": idle,
        "first": first.isoformat(),
        "last": last.isoformat(),
        "mode": mode,
        "mode_hint": mode_hint,
        # 忙碌程度：给界面上一根条
        "level": "high" if density >= 30 else ("mid" if density >= 10 else "low"),
    }


# --------------------------------------------------------------- 重复目录识别
def _tree_signature(path: str, limit: int = 400) -> tuple[set[str], int]:
    """取目录的「文件名集合 + 文件数」，用来判断两个目录是不是同一份东西.

    只比文件名不比内容：内容会随编辑变，但"哪些文件存在"骗不了人。
    跳过依赖/生成物目录 —— 否则两个项目光 node_modules 就长得一样。
    """
    import os

    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
            ".next", ".nuxt", "target", "vendor", ".idea", ".vscode", "coverage",
            "_archive", "outputs", "tmp", ".workbuddy", ".claude"}
    names: set[str] = set()
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for d in dirnames:
            names.add(d + "/")
        for f in filenames:
            names.add(f)
            total += 1
        if total > limit * 4:
            break
    return names, total


def find_duplicates(projects: list[dict]) -> list[dict]:
    """找出「同一个项目被扫成两条」的情况，并给出该信谁的建议.

    用户真实困惑：`demo-app` 和 `demo-app-rel` 都叫「示例英语」，界面上分不清谁是正版。
    系统不该让人靠猜 —— 应该直接说「这俩是同一份东西，A 有 git 更新更近，留 A」。

    判据（按可信度排序）：
      1. 有 .git 的优先（有版本历史，改坏了能回退）
      2. 最后提交 / 目录修改时间更新的优先
      3. 文件更多的优先（不是残缺副本）
      4. 名字更短的优先（`demo-app` vs `demo-app-rel`，后者通常是导出/备份名）
    """
    import os

    by_name: dict[str, list[dict]] = {}
    for p in projects:
        key = (p.get("name") or "").strip().lower()
        if key:
            by_name.setdefault(key, []).append(p)

    out: list[dict] = []
    for name, group in by_name.items():
        if len(group) < 2:
            continue

        # 只保留"像同一份东西"的：文件名集合重合度 ≥ 60%
        sigs: dict[str, tuple[set[str], int]] = {}
        for p in group:
            try:
                sigs[p["path"]] = _tree_signature(p["path"])
            except OSError:
                sigs[p["path"]] = (set(), 0)

        used: set[str] = set()
        for i, a in enumerate(group):
            if a["path"] in used:
                continue
            cluster = [a]
            sa, _na = sigs[a["path"]]
            for b in group[i + 1:]:
                if b["path"] in used:
                    continue
                sb, _nb = sigs[b["path"]]
                if not sa or not sb:
                    continue
                overlap = len(sa & sb) / max(len(sa), len(sb))
                if overlap >= 0.6:
                    cluster.append(b)

            if len(cluster) < 2:
                continue
            used.update(p["path"] for p in cluster)

            def rank(p: dict):
                return (
                    1 if p.get("is_git") else 0,
                    (p.get("last_commit") or "")[:19] or "0",
                    sigs[p["path"]][1],
                    -len(os.path.basename(p["path"])),
                )

            cluster.sort(key=rank, reverse=True)
            keep = cluster[0]
            for p in cluster[1:]:
                reasons = []
                if keep.get("is_git") and not p.get("is_git"):
                    reasons.append("它是 git 仓库，另一个不是")
                kc = (keep.get("last_commit") or "")[:10]
                pc = (p.get("last_commit") or "")[:10]
                if kc and pc and kc > pc:
                    reasons.append(f"更新到 {kc}，另一个停在 {pc}")
                if not kc and not pc:
                    reasons.append("它有更新的修改时间")
                kn = sigs[keep["path"]][1]
                pn = sigs[p["path"]][1]
                if kn > pn:
                    reasons.append(f"文件更全（{kn} vs {pn}）")
                if not reasons:
                    reasons.append("名字更短，通常是主目录")
                out.append({
                    "name": keep.get("name") or name,
                    "keep": keep,
                    "drop": p,
                    "reason": "；".join(reasons[:3]),
                    "overlap": round(
                        len(sigs[keep["path"]][0] & sigs[p["path"]][0])
                        / max(len(sigs[keep["path"]][0]), 1) * 100
                    ),
                })
    return out
