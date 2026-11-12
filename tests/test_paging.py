"""T5 分页纯函数验收."""

from __future__ import annotations

from wb.paging import clamp_page, page_window, paginate


def _items(n: int) -> list[int]:
    return list(range(1, n + 1))


def test_30_items_three_pages():
    chunk, page = paginate(_items(30), 1, 12)
    assert len(chunk) == 12
    assert (page.page, page.pages, page.total) == (1, 3, 30)
    assert (page.start, page.end) == (1, 12)


def test_last_page_partial():
    chunk, page = paginate(_items(30), 3, 12)
    assert len(chunk) == 6
    assert (page.start, page.end) == (25, 30)


def test_page_beyond_range_clamps_to_last():
    chunk, page = paginate(_items(30), 99, 12)
    assert page.page == 3
    assert len(chunk) == 6, "越界不该返回空表"


def test_page_zero_and_negative_clamp_to_first():
    assert paginate(_items(30), 0, 12)[1].page == 1
    assert paginate(_items(30), -5, 12)[1].page == 1


def test_page_non_numeric_clamps_to_first():
    assert clamp_page("abc", 5) == 1
    assert clamp_page(None, 5) == 1


def test_empty_list_one_page():
    chunk, page = paginate([], 1, 12)
    assert chunk == []
    assert (page.page, page.pages, page.total) == (1, 1, 0)


def test_exact_multiple_no_empty_tail():
    _, page = paginate(_items(24), 2, 12)
    assert page.pages == 2
    assert page.end == 24


def test_page_window_small():
    assert page_window(1, 3) == [1, 2, 3]
    assert page_window(2, 7) == [1, 2, 3, 4, 5, 6, 7]


def test_page_window_large_middle():
    assert page_window(5, 20) == [1, None, 4, 5, 6, None, 20]


def test_page_window_first_and_last():
    assert page_window(1, 20) == [1, 2, None, 20]
    assert page_window(20, 20) == [1, None, 19, 20]
