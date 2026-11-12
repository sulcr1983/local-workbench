"""操作记录（审计）页：本机所有写操作的流水，出问题能倒查."""

from __future__ import annotations

import csv
import io as _io

from flask import Blueprint, Response, render_template, request, url_for

from .. import security

from .. import db
from ..paging import paginate

bp = Blueprint("audit", __name__)

PER_PAGE = 20

# 动作名 → 徽章色，只为了一眼区分「改了什么」
ACTION_KIND = {
    "添加": "ok", "修改": "info", "删除": "err", "导出": "mut",
    "扫描": "mut", "刷新": "info", "置顶": "brand", "批量": "warn",
}


def _kind(action: str) -> str:
    for key, val in ACTION_KIND.items():
        if key in action:
            return val
    return "mut"


@bp.get("/audit")
def index():
    rows = db.query("SELECT * FROM audit ORDER BY id DESC")
    chunk, page = paginate(rows, request.args.get("page"), PER_PAGE)

    def mk_url(n: int) -> str:
        return url_for("audit.index", page=n)

    items = [
        {"id": r["id"], "ts": r["ts"], "action": r["action"], "detail": r["detail"] or "",
         "kind": _kind(r["action"])}
        for r in chunk
    ]

    return render_template(
        "audit.html",
        page_title="操作记录",
        page_code="AUDIT / 006",
        crumb="操作记录",
        active="audit",
        items=items,
        page=page,
        total_all=len(rows),
        mk_url=mk_url,
        counts={},
    )


@bp.get("/export/audit.csv")
def export_csv():
    """把操作记录导成 CSV；字段做公式注入防护."""
    rows = db.query("SELECT * FROM audit ORDER BY id DESC")
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "操作", "对象", "结果"])
    for r in rows:
        w.writerow([
            security.csv_safe(r["ts"]),
            security.csv_safe(r["action"]),
            security.csv_safe(r["detail"] or ""),
            "成功",
        ])
    db.audit("导出操作记录", f"{len(rows)} 条")
    return Response(
        "\ufeff" + buf.getvalue(),
        # 用 content_type 一次给全，避免 charset 重复（见 projects.export_csv 注释）
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
    )
