"""分页：纯函数，不碰 DB、不碰模板.

口径（TECH_SPEC §7.8）：越界夹紧、pages<=1 隐藏、页码窗口 1 … n-1 n n+1 … N。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PER = 12


@dataclass(frozen=True)
class Page:
    page: int          # 当前页（已夹紧）
    pages: int         # 总页数（至少 1）
    total: int         # 总条数
    per: int           # 每页条数
    start: int         # 本页第一条的序号（1 起，空页为 0）
    end: int           # 本页最后一条的序号

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def clamp_page(page: int | str | None, pages: int) -> int:
    try:
        n = int(page)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, max(1, pages)))


def paginate(items: list, page: int | str | None = 1, per: int = DEFAULT_PER) -> tuple[list, Page]:
    """切片 + 页码信息；?page=99 自动落到末页，不返回空表."""
    per = max(1, int(per))
    total = len(items)
    pages = max(1, (total + per - 1) // per)
    cur = clamp_page(page, pages)
    start = (cur - 1) * per
    chunk = items[start : start + per]
    return chunk, Page(
        page=cur,
        pages=pages,
        total=total,
        per=per,
        start=start + 1 if chunk else 0,
        end=start + len(chunk),
    )


def page_window(page: int, pages: int, span: int = 1) -> list[int | None]:
    """页码窗口；None 表示省略号."""
    if pages <= 7:
        return list(range(1, pages + 1))

    lo = max(2, page - span)
    hi = min(pages - 1, page + span)
    out: list[int | None] = [1]
    if lo > 2:
        out.append(None)
    out.extend(range(lo, hi + 1))
    if hi < pages - 1:
        out.append(None)
    out.append(pages)
    return out
