"""项目档案：列表 / 筛选 / 排序 / 分页.

T5 范围：只读展示 + 扫描落库 + 根目录最小入口。
写操作（改名、置顶、批量）在 T7，详情页在 T6。
"""

from __future__ import annotations

import base64
import csv
import io
import os
import re

from flask import (
    Blueprint, Response, abort, jsonify, redirect, render_template, request, send_file, url_for,
)

from .. import db, scan, security, viz
from ..paging import DEFAULT_PER, paginate

bp = Blueprint("projects", __name__)

# 截图：只认这一张，覆盖式保存；上限 8MB（TECH_SPEC §6.2）
SHOT_NAME = "preview.png"
SHOT_MAX = 8 * 1024 * 1024
DATA_URL_RE = re.compile(r"^data:image/(png|jpe?g|webp);base64,(.+)$", re.IGNORECASE | re.DOTALL)

# README 内引用的本地资源（图片 + 相对文档链接）。
# 只放行固定扩展名，避免这个路由变成「读项目里任意文件」的口子：
# .env / .db / 私钥 / 密钥文件一律 404。SVG 是本地自产物，按用户口径放行，
# 但单独加 CSP + nosniff，防止直接新标签打开时执行内嵌脚本。
README_ASSET_IMAGES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".ico", ".svg",
}
README_ASSET_TEXT = {".md", ".markdown", ".txt"}
README_ASSET_EXTS = README_ASSET_IMAGES | README_ASSET_TEXT

# 筛选与排序的口径写在这里，模板只负责画
FLTS = {
    "all": "全部",
    "active": "活跃",
    "stale": "停滞",
    "dirty": "有改动",
    "pinned": "置顶",
    "archived": "已归档",
    "ignored": "已忽略",
}
SORTS = {"recent": "最近动过", "name": "名称", "conf": "待确认"}

AVATAR_COLORS = ["#2F6FED", "#2E7D32", "#B26A00", "#A32D2D", "#7F5AF0", "#0E7490", "#8A5A83", "#2C5147"]


def avatar(name: str) -> tuple[str, str]:
    """首字母 + 稳定配色（同名永远同色，不随排序变）."""
    ch = (name.strip()[:1] or "?").upper()
    idx = sum(ord(c) for c in name) % len(AVATAR_COLORS)
    return ch, AVATAR_COLORS[idx]


def _roots() -> list[str]:
    return [r["path"] for r in db.query("SELECT path FROM root ORDER BY added_at")]


def _display(row) -> dict:
    """行 → 模板要的视图模型；手工覆盖优先于自动解析."""
    name = row["user_name"] or row["name_auto"] or row["dirname"]
    ch, color = avatar(name)
    auto_active = row["active"] or "未知"
    _repo = gh_url(row["remote"] or "")
    return {
        "path": row["path"],
        "name": name,
        "name_src": row["name_src"],
        "name_conf": row["name_conf"],
        "name_overridden": bool(row["user_name"]),
        "dirname": row["dirname"],
        "stack": row["stack"] or "未识别",
        "active": auto_active,
        # 手工状态（进行中/已完成/归档）压过自动活跃度
        "status": row["user_status"] or auto_active,
        "is_git": bool(row["is_git"]),
        "last_commit": row["last_commit"],
        "dirty": bool(row["dirty"]),
        "pinned": bool(row["user_pinned"]),
        "note": row["user_note"] or "",
        "summary": row["summary"] or "",
        "dirty_n": row["dirty_n"] or 0,
        "unpushed_n": row["unpushed_n"] or 0,
        "remote": row["remote"] or "",
        "servers": [],
        "run_local": bool(row["run_local"]) if "run_local" in row.keys() else False,
        "rel": "",                       # 路由用：相对根目录的路径
        "rel_color": REL_COLOR.get(auto_active, "#6E7681"),
        "rel_time_text": rel_time(row["last_commit"]),
        "gh_url": _repo,
        "gh_site": repo_site(_repo),
        # 引擎判定：auto 收编 / candidate 候选 / dropped 减掉。
        # claimed = 人碰过（改过名、设过状态、置顶），人工认可的一律算数。
        "state": (row["state"] if "state" in row.keys() else "auto") or "auto",
        "claimed": bool(row["user_name"] or row["user_status"] or row["user_pinned"]),
        "avatar": ch,
        "color": color,
    }


def _filter(items: list[dict], flt: str, q: str) -> list[dict]:
    if flt != "ignored":
        items = [i for i in items if i["status"] != "忽略"]  # 默认不显示"已忽略"
    if flt == "active":
        items = [i for i in items if i["status"] == "活跃"]
    elif flt == "stale":
        items = [i for i in items if i["status"] in ("放缓", "停滞", "可能废弃")]
    elif flt == "dirty":
        items = [i for i in items if i["dirty"]]
    elif flt == "pinned":
        items = [i for i in items if i["pinned"]]
    elif flt == "archived":
        items = [i for i in items if i["status"] == "归档"]
    elif flt == "ignored":
        items = [i for i in items if i["status"] == "忽略"]

    if q:
        needle = q.lower()
        items = [
            i for i in items
            if needle in i["name"].lower()
            or needle in i["dirname"].lower()
            or needle in i["path"].lower()
        ]
    return items

    if q:
        needle = q.lower()
        items = [
            i for i in items
            if needle in i["name"].lower()
            or needle in i["dirname"].lower()
            or needle in i["path"].lower()
        ]
    return items


def _sort(items: list[dict], sort: str) -> list[dict]:
    if sort == "name":
        return sorted(items, key=lambda i: i["name"].lower())
    if sort == "conf":
        # 置信度低的排前面 —— 这一屏就是给人工确认用的
        return sorted(items, key=lambda i: (i["name_conf"] if i["name_conf"] is not None else 1.0))
    return sorted(
        items,
        key=lambda i: (i["pinned"], i["last_commit"] or ""),
        reverse=True,
    )


REL_COLOR = {"活跃": "#2E7D32", "放缓": "#8A5200", "停滞": "#8A5200", "可能废弃": "#A32D2D", "未知": "#6E7681"}


def rel_time(ts: str | None) -> str:
    """最后提交 → 「今天 / 3 天前 / 约 2 个月前」。"""
    if not ts:
        return "—"
    from datetime import datetime as _dt

    try:
        when = _dt.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "—"
    days = (_dt.now() - when).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days} 天前"
    if days < 30:
        return f"{days // 7} 周前"
    if days < 365:
        return f"约 {days // 30} 个月前"
    return f"约 {days // 365} 年前"


def gh_url(remote: str) -> str:
    """git remote → 能点开的仓库地址.

    原来只认 github.com，自建 GitLab / Gitee / Coding 全都拿不到链接。
    现在交给 scan.repo_url 统一处理（认 git@ / ssh:// / https:// 三种写法）。
    """
    from ..scan import repo_url

    return repo_url(remote)


def repo_site(url: str) -> str:
    """链接的站点短名，用来决定按钮写「GitHub」还是「Gitee」。"""
    low = (url or "").lower()
    for key, label in (
        ("github.com", "GitHub"),
        ("gitee.com", "Gitee"),
        ("gitlab", "GitLab"),
        ("coding.net", "Coding"),
        ("bitbucket", "Bitbucket"),
        ("codeup", "Codeup"),
    ):
        if key in low:
            return label
    return "代码仓库"


@bp.get("/projects")
def index():
    q = (request.args.get("q") or "").strip()
    flt = request.args.get("flt") or "all"
    sort = request.args.get("sort") or "recent"
    if flt not in FLTS:
        flt = "all"
    if sort not in SORTS:
        sort = "recent"

    roots = _roots()
    rows = db.query("SELECT * FROM project ORDER BY last_seen DESC")
    all_items = [_display(r) for r in rows]
    # 引擎减掉的（候选 / 已剔除）不进列表 —— 但人碰过的一律算数，不能被引擎藏起来
    all_items = [i for i in all_items if i["state"] == "auto" or i["claimed"]]

    # 项目 → 部署到哪台（一次查完，别在循环里查）
    links: dict[str, list[str]] = {}
    for link in db.query(
        """SELECT ps.project_path AS p, s.name AS n FROM project_server ps
           JOIN server s ON s.id = ps.server_id ORDER BY s.name"""
    ):
        links.setdefault(link["p"], []).append(link["n"])
    for it in all_items:
        it["servers"] = links.get(it["path"], [])

    # 一次算好 rel（别在循环里反复查 root 表）
    for it in all_items:
        for root in roots:
            if security.is_within(root, it["path"]):
                it["rel"] = os.path.relpath(it["path"], root).replace("\\", "/")
                break
        else:
            it["rel"] = os.path.basename(it["path"])

    dist_counts = {"活跃": 0, "放缓 / 停滞": 0, "可能废弃": 0, "未分类": 0}
    for it in all_items:
        st = it["status"]
        if st == "活跃":
            dist_counts["活跃"] += 1
        elif st in ("放缓", "停滞"):
            dist_counts["放缓 / 停滞"] += 1
        elif st == "可能废弃":
            dist_counts["可能废弃"] += 1
        else:
            dist_counts["未分类"] += 1
    dist = [
        ("活跃", dist_counts["活跃"], "#2E7D32"),
        ("放缓 / 停滞", dist_counts["放缓 / 停滞"], "#B26A00"),
        ("可能废弃", dist_counts["可能废弃"], "#A32D2D"),
        ("未分类", dist_counts["未分类"], "#9AA1AB"),
    ]

    # ---------------------------------------------------------- 页头可视化
    # 四个大数回答「我这堆项目整体什么状况」，比读表格快得多
    from .. import viz

    total = len(all_items)
    stats = [
        {"k": "项目总数", "v": total, "s": "在册", "icon": "folder", "color": "#2F6FED"},
        {"k": "还在动", "v": dist_counts["活跃"], "s": "7 天内有提交", "icon": "activity", "color": "#2E7D32"},
        {"k": "有未提交", "v": sum(1 for i in all_items if i["dirty_n"]), "s": "改动没落盘",
         "icon": "alert", "color": "#B26A00"},
        {"k": "没写 README", "v": sum(1 for i in all_items if not i["summary"]),
         "s": "别人看不懂", "icon": "book", "color": "#A32D2D"},
    ]

    # 技术栈分布（用真实计数，不估算）。只画前 4 种，其余并成「其他」——
    # 7 行的分布图会把项目列表整个挤到折叠线以下，违背「摘要」的本意。
    stack_count: dict[str, int] = {}
    for i in all_items:
        stack_count[i["stack"]] = stack_count.get(i["stack"], 0) + 1
    stack_sorted = sorted(stack_count.items(), key=lambda kv: -kv[1])
    stack_rows = []
    other_n = 0
    for k, v in stack_sorted:
        if len(stack_rows) < 4:
            stack_rows.append({"label": k, "n": v, "key": viz.stack_key(k)})
        else:
            other_n += v
    if other_n:
        stack_rows.append({"label": "其他", "n": other_n, "key": "none"})
    stack_peak = max((r["n"] for r in stack_rows), default=0)
    for r in stack_rows:
        r["pct"] = int(round(r["n"] / stack_peak * 100)) if stack_peak else 0

    items = _sort(_filter(all_items, flt, q), sort)
    chunk, page = paginate(items, request.args.get("page"), DEFAULT_PER)

    def mk_url(n: int) -> str:
        return url_for("projects.index", q=q or None, flt=flt, sort=sort, page=n)

    return render_template(
        "projects.html",
        page_title="项目档案",
        page_code="PROJECTS / 002",
        crumb="项目档案",
        active="projects",
        items=chunk,
        page=page,
        total_all=len(all_items),
        roots=roots,
        q=q,
        flt=flt,
        sort=sort,
        flts=FLTS,
        sorts=SORTS,
        mk_url=mk_url,
        dist=dist,
        stats=stats,
        stack_rows=stack_rows,
        duplicates=_dups_with_rel(viz.find_duplicates(all_items), all_items),
        counts={"projects": len(all_items)},
    )


def _dups_with_rel(dups: list[dict], items: list[dict]) -> list[dict]:
    """给重复项补 rel —— 模板要拿 rel 生成详情页链接."""
    rel_of_path = {i["path"]: i["rel"] for i in items}
    for d in dups:
        for side in ("keep", "drop"):
            p = d[side]
            p["rel"] = rel_of_path.get(p["path"], p.get("dirname", ""))
    return dups


@bp.post("/projects/scan")
def scan_now():
    roots = _roots()
    if roots:
        result = scan.sync(roots)
        db.audit("扫描项目", f"{result['scanned']} 个 · {', '.join(roots)}")
    return redirect(url_for("projects.index"))


@bp.post("/projects/root")
def add_root():
    path = (request.form.get("path") or "").strip().strip('"')
    if not path:
        return redirect(url_for("projects.index"))
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return redirect(url_for("projects.index", err="notdir"))
    with db.tx() as c:
        c.execute(
            "INSERT INTO root(path, added_at) VALUES(?,?) ON CONFLICT(path) DO NOTHING",
            (path, db.now()),
        )
    db.audit("添加根目录", path)
    scan.sync([path])
    return redirect(url_for("projects.index"))


# ------------------------------------------------------------------ 详情
def lookup(rel: str):
    """rel（相对根目录）→ (project 行, 所属根)。越界或不存在的都返回 (None, None)."""
    for root in _roots():
        target = security.resolve_under(root, rel)
        if not target:
            continue
        row = db.query_one("SELECT * FROM project WHERE path=?", (target,))
        if row:
            return row, root
    return None, None


def rel_of(path: str) -> str:
    """绝对路径 → 相对某个根的 rel（用于生成链接）。找不到就退回目录名。"""
    for root in _roots():
        if security.is_within(root, path):
            return os.path.relpath(path, root).replace("\\", "/")
    return os.path.basename(path)


def _readme_view(row, rel: str) -> str | None:
    """README 渲染：hash 相同用缓存，变了才重渲（TECH_SPEC §7.7）.

    rel 同时作为本地资源 URL 的基准 —— README 里的相对图片/链接要改写到
    /readme-asset/<rel>，否则浏览器会按 /project/<rel> 去拼，必然 404。
    """
    md_path = scan.find_readme(row["path"])
    if not md_path:
        return None
    raw = scan.read_text(md_path)
    if not raw.strip():
        return None
    digest = security.readme_digest(raw)
    if row["readme"] and row["readme_hash"] == digest:
        return row["readme"]

    html = security.render_markdown(raw, asset_base=rel)
    db.execute(
        "UPDATE project SET readme=?, readme_hash=? WHERE path=?",
        (html, digest, row["path"]),
    )
    return html


@bp.get("/project/<path:rel>")
def detail(rel: str):
    row, _root = lookup(rel)
    if row is None:
        abort(404)

    p = _display(row)
    p["last_commit"] = row["last_commit"]
    p["rel"] = rel
    p["first_seen"] = row["first_seen"]
    p["last_seen"] = row["last_seen"]
    p["branch"] = ""

    # host/port 一起取：部署弹窗要显示真实地址，只 select id,name 会渲染成空的「:」
    all_servers = db.query("SELECT id, name, host, port FROM server ORDER BY name")
    linked = {
        r["server_id"]
        for r in db.query("SELECT server_id FROM project_server WHERE project_path=?", (row["path"],))
    }
    # 模板里直接列名字（还要能点进服务器详情），在这儿算好，别在 Jinja 里拼分隔符
    linked_servers = [s for s in all_servers if s["id"] in linked]

    # ---------------------------------------------------------- 可视化数据
    # 原则：只把已读到的信息换个形状，不编数字。取不到就明说取不到。
    from .. import viz

    commits = scan.git_commits(row["path"])
    for c in commits:
        c.update(viz.commit_kind(c["subject"]))
    has_readme = bool(scan.find_readme(row["path"]))
    hp = viz.health(p, has_readme=has_readme, linked_n=len(linked))

    # 提交节奏必须按**时间窗口**取，不能复用上面那 15 条 ——
    # 15 条是采样，活跃项目一天就 25 条，拿它算周计数会算少（实测 53 算成 10）。
    span_commits = scan.git_commits_since(row["path"], days=56)
    buckets = viz.week_buckets(span_commits, weeks=8)

    # 体检 + 工作节奏：PM 视角的两块。
    # 待办债（扫 TODO 注释）已砍掉 —— 字面匹配误报率太高（实测 24 条里 22 条是
    # 用法说明里的占位符），一个不准的面板会让人怀疑整页数据。
    # 换成 cadence：全部是 git 里的硬事实，不推测。
    risk_rows = viz.risks(p, has_readme=has_readme, linked_n=len(linked))
    work = viz.cadence(scan.git_commits_since(row["path"], days=180))

    # 提交类型分布（只在有提交时才有意义）
    # 类型分布也用时间窗口那批 —— 跟柱子同一分母，否则"柱子 185 条、类型合计 15 条"自相矛盾
    kind_count: dict[str, int] = {}
    meta: dict[str, dict] = {}
    for c in span_commits:
        k = viz.commit_kind(c["subject"])
        kind_count[k["kind"]] = kind_count.get(k["kind"], 0) + 1
        meta[k["kind"]] = k
    kind_rows = sorted(
        ({"kind": k, "n": n, "label": meta[k]["label"],
          "color": meta[k]["color"], "icon": meta[k]["icon"]}
         for k, n in kind_count.items()),
        key=lambda r: -r["n"],
    )

    return render_template(
        "project_detail.html",
        page_title=p["name"],
        page_code="PROJECT DETAIL",
        crumb=p["name"],
        crumb_sub=None,
        active="projects",
        p=p,
        rel=rel,
        readme_html=_readme_view(row, rel),
        commits=commits,
        health=hp,
        weeks=buckets,
        kind_rows=kind_rows,
        work=work,
        risks=risk_rows,
        stack_key=viz.stack_key(p["stack"]),
        has_readme=has_readme,
        raw=row,
        all_servers=all_servers,
        linked=linked,
        linked_servers=linked_servers,
        has_preview=bool(row["preview"]),
        shot_name=row["preview"] or "",
        counts={},
    )


@bp.get("/preview/<path:rel>")
def preview(rel: str):
    row, _root = lookup(rel)
    if row is None or not row["preview"]:
        abort(404)
    target = security.resolve_under(row["path"], row["preview"])
    if not target or not os.path.isfile(target):
        abort(404)
    return send_file(target, max_age=0, conditional=True)


@bp.get("/readme-asset/<path:rel>")
def readme_asset(rel: str):
    """服务 README 里引用的项目内资源（相对图片 / 相对文档链接）.

    安全口径与 /preview 一致：先用 lookup 定位项目，再用 resolve_under 把路径
    锁死在项目根目录内（拒 .. / 绝对路径 / 盘符），最后卡扩展名白名单。
    f 参数只接受相对路径，中文文件名由浏览器 URL 解码后照常读取。
    """
    row, _root = lookup(rel)
    if row is None:
        abort(404)
    f = request.args.get("f", "")
    if not f:
        abort(404)
    target = security.resolve_under(row["path"], f)
    if not target or not os.path.isfile(target):
        abort(404)
    ext = os.path.splitext(target)[1].lower()
    if ext not in README_ASSET_EXTS:
        abort(404)

    if ext in README_ASSET_TEXT:
        # 本地没有 GitHub 那种「文件页」，如实给原始文本，不假装渲染成网页
        # 只给 text/plain，charset 由 Flask 自动补一次（写全会出现重复的 charset）
        resp = send_file(target, mimetype="text/plain", max_age=0, conditional=True)
    elif ext == ".svg":
        resp = send_file(target, mimetype="image/svg+xml",
                         max_age=0, conditional=True)
    else:
        resp = send_file(target, max_age=0, conditional=True)

    resp.headers["X-Content-Type-Options"] = "nosniff"
    if ext == ".svg":
        # img 上下文里 SVG 本来就不跑脚本；这里防的是用户直接新标签打开 SVG
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        )
    return resp


# -------------------------------------------------------------- 截图写入
def _fail(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


@bp.post("/api/project/preview")
def api_preview_upload():
    """Ctrl+V / 选文件都走这里；base64 直传，不落临时文件."""
    rel = (request.form.get("rel") or "").strip()
    data_url = request.form.get("data") or ""
    row, _root = lookup(rel)
    if row is None:
        return _fail("项目不存在，可能已经被移走了")

    m = DATA_URL_RE.match(data_url)
    if not m:
        return _fail("只认 PNG / JPG / WebP 图片")
    try:
        blob = base64.b64decode(m.group(2), validate=True)
    except (ValueError, TypeError):
        return _fail("图片数据不完整，重新复制一次试试")
    if not blob:
        return _fail("图片是空的")
    if len(blob) > SHOT_MAX:
        return _fail(f"图片 {len(blob) / 1024 / 1024:.1f}MB，超过 {SHOT_MAX // 1024 // 1024}MB 上限")

    target = os.path.join(row["path"], SHOT_NAME)
    try:
        with open(target, "wb") as f:
            f.write(blob)
    except OSError as err:
        return _fail(f"写文件失败：{err}", 500)

    db.execute("UPDATE project SET preview=? WHERE path=?", (SHOT_NAME, row["path"]))
    db.audit("更新截图", f"{row['dirname']} · {len(blob) // 1024}KB")
    return jsonify({"ok": True, "preview": SHOT_NAME, "size": len(blob)})


@bp.post("/api/project/preview/delete")
def api_preview_delete():
    rel = (request.form.get("rel") or "").strip()
    row, _root = lookup(rel)
    if row is None:
        return _fail("项目不存在")

    name = row["preview"] or ""
    # 只删我们自己认的那张图；用户自己的截图文件不动
    if name == SHOT_NAME:
        target = os.path.join(row["path"], SHOT_NAME)
        if os.path.isfile(target):
            try:
                os.remove(target)
            except OSError as err:
                return _fail(f"删除失败：{err}", 500)
    db.execute("UPDATE project SET preview=NULL WHERE path=?", (row["path"],))
    db.audit("删除截图", row["dirname"])
    return jsonify({"ok": True})


# ------------------------------------------------------------------ 导出
CSV_HEADER = ["名称", "路径", "技术栈", "状态", "最后提交", "未提交", "名称来源", "置信度", "置顶", "备注"]
SRC_TEXT = {"readme_h1": "README 标题", "manifest": "清单文件", "dirname": "目录名"}


@bp.get("/export/projects.csv")
def export_csv():
    """导给程序消费的 CSV；公式注入按 security.csv_safe 的口径处理."""
    rows = db.query("SELECT * FROM project ORDER BY user_pinned DESC, last_seen DESC")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows:
        d = _display(r)
        writer.writerow([
            security.csv_safe(d["name"]),
            security.csv_safe(d["path"]),
            security.csv_safe(d["stack"]),
            security.csv_safe(d["status"]),
            security.csv_safe(d["last_commit"] or ""),
            "是" if d["dirty"] else "否",
            SRC_TEXT.get(d["name_src"], "目录名"),
            d["name_conf"] if d["name_conf"] is not None else "",
            "是" if d["pinned"] else "否",
            security.csv_safe(d["note"]),
        ])
    body = "\ufeff" + buf.getvalue()  # BOM：让 Excel 正确识别 UTF-8
    db.audit("导出 CSV", f"{len(rows)} 条")
    return Response(
        body,
        # 用 content_type 一次给全：mimetype= 会把 "text/csv; charset=utf-8"
        # 当成 MIME 类型再补一遍 charset，头部就变成 charset 重复两次。
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="projects.csv"'},
    )
