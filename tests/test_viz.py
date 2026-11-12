"""可视化纯函数验收：提交分类 / 周桶 / 健康度.

这些函数只做「把已有数据换个形状」，所以测试重点有两个：
1. 形状算得对（边界、空输入、跨周）
2. **不编数据** —— 取不到就是取不到，不能硬凑一个好看的数字
"""

from __future__ import annotations

import os
from datetime import date

from wb import viz

# --------------------------------------------------------------- 提交分类
def test_commit_kind_reads_conventional_prefix():
    assert viz.commit_kind("fix(ssh): 密钥按最新的先用")["kind"] == "fix"
    assert viz.commit_kind("feat(servers): 标题行即入口")["kind"] == "feat"
    assert viz.commit_kind("docs: 补 README")["kind"] == "docs"
    assert viz.commit_kind("chore: 清理临时文件")["kind"] == "chore"


def test_commit_kind_reads_chinese_prefix():
    assert viz.commit_kind("修复登录失败")["kind"] == "fix"
    assert viz.commit_kind("新增导出功能")["kind"] == "feat"
    assert viz.commit_kind("优化查询速度")["kind"] == "perf"
    assert viz.commit_kind("重构扫描逻辑")["kind"] == "refactor"


def test_commit_kind_reads_bracket_form():
    assert viz.commit_kind("[feat] 加一个按钮")["kind"] == "feat"
    assert viz.commit_kind("【修复】表单校验")["kind"] == "fix"


def test_commit_kind_gives_each_kind_an_icon_and_color():
    for subject in ("feat: a", "fix: b", "docs: c", "unknown stuff", ""):
        got = viz.commit_kind(subject)
        assert got["icon"], subject
        assert got["color"].startswith("#"), subject
        assert got["label"], subject


def test_commit_kind_falls_back_to_other_without_guessing():
    """认不出就归 other —— 猜错比不猜更烦人."""
    got = viz.commit_kind("update stuff")
    assert got["kind"] == "other"
    assert viz.commit_kind("")["kind"] == "other"


# --------------------------------------------------------------- 周桶
def test_week_buckets_puts_commits_in_right_week():
    today = date(2026, 11, 11)  # 周三
    commits = [
        {"date": "2026-11-11"},  # 本周
        {"date": "2026-11-10"},  # 本周
        {"date": "2026-11-04"},  # 上周
    ]
    buckets = viz.week_buckets(commits, weeks=8, today=today)
    # 前导空周会被裁掉，只留最近一段（至少保留 1 个空周作参照）
    assert buckets[-1]["count"] == 2, buckets[-1]
    assert buckets[-2]["count"] == 1, buckets[-2]
    assert len(buckets) <= 8


def test_week_buckets_trims_leading_empty_weeks():
    """8 周里只有最后 2 周有数据时，不该用 6 个空槽把图撑开."""
    today = date(2026, 11, 11)
    commits = [{"date": "2026-11-11"}, {"date": "2026-11-05"}]
    buckets = viz.week_buckets(commits, weeks=8, today=today)
    assert len(buckets) <= 4, [b["label"] for b in buckets]
    # 至少保留一个空周，让人看到"上上周也没动"
    assert buckets[0]["count"] == 0, buckets


def test_week_buckets_keeps_full_window_when_dense():
    """8 周都有数据时不该裁，全画出来."""
    today = date(2026, 11, 11)
    commits = [{"date": f"2026-{m:02d}-{d:02d}"} for m in (9, 10, 11) for d in (1, 8, 15, 22, 29)]
    buckets = viz.week_buckets(commits, weeks=8, today=today)
    assert len(buckets) == 8, [b["label"] for b in buckets]


def test_week_buckets_ignores_commits_outside_window():
    today = date(2026, 11, 11)
    commits = [{"date": "2026-01-01"}, {"date": "2026-11-11"}]
    buckets = viz.week_buckets(commits, weeks=4, today=today)
    assert sum(b["count"] for b in buckets) == 1


def test_week_buckets_marks_empty_weeks_as_zero_not_tiny():
    """空周必须是 0，不能给个"最小高度"冒充有数据."""
    today = date(2026, 11, 11)
    buckets = viz.week_buckets([{"date": "2026-11-11"}], weeks=4, today=today)
    empties = [b for b in buckets if b["count"] == 0]
    assert empties, "这个窗口里应该有空的周"
    assert all(b["pct"] == 0 for b in empties), empties


def test_week_buckets_handles_bad_dates():
    today = date(2026, 11, 11)
    buckets = viz.week_buckets([{"date": ""}, {"date": "不是日期"}, {}], weeks=4, today=today)
    assert sum(b["count"] for b in buckets) == 0


def test_week_buckets_peak_is_hundred_percent():
    today = date(2026, 11, 11)
    commits = [{"date": "2026-11-11"}] * 5 + [{"date": "2026-11-04"}]
    buckets = viz.week_buckets(commits, weeks=2, today=today)
    assert buckets[-1]["pct"] == 100
    assert buckets[-2]["pct"] == 20


# --------------------------------------------------------------- 健康度
def _proj(**kw):
    base = {"is_git": True, "dirty_n": 0, "unpushed_n": 0,
            "last_commit": "2026-11-11 10:00:00", "run_local": 0}
    base.update(kw)
    return base


def test_health_all_good_scores_high():
    h = viz.health(_proj(), has_readme=True, linked_n=1, today=date(2026, 11, 11))
    assert h["score"] == 100, h
    assert h["grade"] == "健康"


def test_health_flags_missing_readme():
    h = viz.health(_proj(), has_readme=False, linked_n=1, today=date(2026, 11, 11))
    doc = next(p for p in h["parts"] if p["key"] == "doc")
    assert doc["pct"] == 0 and "README" in doc["text"]


def test_health_flags_dirty_files():
    h = viz.health(_proj(dirty_n=16), has_readme=True, linked_n=1, today=date(2026, 11, 11))
    sync = next(p for p in h["parts"] if p["key"] == "sync")
    assert sync["pct"] < 50, sync
    assert "16" in sync["text"]


def test_health_flags_dirty_and_unpushed_together():
    """两个问题同时存在要比单个更严重 —— 不能只报其中一个."""
    h = viz.health(_proj(dirty_n=3, unpushed_n=2), has_readme=True, linked_n=0,
                   today=date(2026, 11, 11))
    sync = next(p for p in h["parts"] if p["key"] == "sync")
    assert "3" in sync["text"] and "2" in sync["text"], sync["text"]


def test_health_flags_stale_project():
    h = viz.health(_proj(last_commit="2025-01-01"), has_readme=True, linked_n=1,
                   today=date(2026, 11, 11))
    act = next(p for p in h["parts"] if p["key"] == "active")
    assert act["pct"] <= 10
    assert h["score"] < 80


def test_health_says_no_commit_instead_of_faking_zero_days():
    """没有提交记录时不能算成「0 天前动过」—— 那是编数据."""
    h = viz.health(_proj(last_commit=""), has_readme=True, linked_n=1, today=date(2026, 11, 11))
    act = next(p for p in h["parts"] if p["key"] == "active")
    assert act["pct"] == 0
    assert "没有提交" in act["text"], act["text"]


def test_health_deploy_counts_local_and_servers():
    h = viz.health(_proj(run_local=1), has_readme=True, linked_n=2, today=date(2026, 11, 11))
    dep = next(p for p in h["parts"] if p["key"] == "deploy")
    assert dep["pct"] == 100
    assert "本机" in dep["text"] and "2" in dep["text"]


def test_health_deploy_zero_when_nowhere():
    h = viz.health(_proj(run_local=0), has_readme=True, linked_n=0, today=date(2026, 11, 11))
    dep = next(p for p in h["parts"] if p["key"] == "deploy")
    assert dep["pct"] == 0


def test_health_non_git_scores_zero_on_sync():
    h = viz.health(_proj(is_git=False), has_readme=True, linked_n=1, today=date(2026, 11, 11))
    sync = next(p for p in h["parts"] if p["key"] == "sync")
    assert sync["pct"] == 0
    assert "不是 git" in sync["text"]


def test_health_always_has_four_parts():
    """四维固定，界面上是按 4 列排的 —— 少一个就塌."""
    for kw in ({}, {"is_git": False}, {"last_commit": ""}):
        h = viz.health(_proj(**kw), has_readme=False, linked_n=0, today=date(2026, 11, 11))
        assert len(h["parts"]) == 4, h["parts"]
        assert {p["key"] for p in h["parts"]} == {"active", "sync", "doc", "deploy"}


def test_health_parts_key_is_not_items():
    """键名不能叫 items —— Jinja 里 health.items 会取到 dict 内置方法."""
    h = viz.health(_proj(), has_readme=True, linked_n=1)
    assert "items" not in h
    assert "parts" in h


# --------------------------------------------------------------- 待办债
def test_risks_flags_uncommitted_and_unpushed():
    p = {"is_git": True, "dirty_n": 25, "unpushed_n": 0, "last_commit": "2026-11-11",
         "remote": "origin", "run_local": 1}
    r = viz.risks(p, has_readme=True, linked_n=0, )
    titles = [x["t"] for x in r]
    assert any("没提交" in t for t in titles), titles


def test_risks_has_action_for_each():
    """每条体检项都要带动作 —— PM 要的是「去干什么」，不是一句评价."""
    p = {"is_git": True, "dirty_n": 0, "unpushed_n": 0, "last_commit": "2025-01-01",
         "remote": "", "run_local": 0}
    r = viz.risks(p, has_readme=False, linked_n=0, )
    assert r, "这种项目不该体检出「没问题」"
    for x in r:
        assert x["act"], f"{x['t']} 缺动作"
        assert x["lv"] in ("err", "warn", "mut", "ok")


def test_risks_ok_when_all_clean():
    p = {"is_git": True, "dirty_n": 0, "unpushed_n": 0, "last_commit": "2026-11-11",
         "remote": "origin", "run_local": 1}
    r = viz.risks(p, has_readme=True, linked_n=1, )
    assert len(r) == 1 and r[0]["lv"] == "ok", r


def test_risks_red_first_ordering():
    """会丢东西的（红）必须排最前 —— PM 先看会出事的."""
    p = {"is_git": True, "dirty_n": 30, "unpushed_n": 0, "last_commit": "2025-01-01",
         "remote": "", "run_local": 0}
    r = viz.risks(p, has_readme=False, linked_n=0, )
    # 红(没提交)要在黄(没README)前面
    lvs = [x["lv"] for x in r]
    assert lvs[0] == "err", lvs


# --------------------------------------------------------------- 工作节奏
def _c(dates):
    return [{"date": d, "subject": "feat: x"} for d in dates]


def test_cadence_counts_from_full_window_not_sample():
    """节奏必须按时间窗口算，不能拿「最近 15 条」采样 —— 那是之前算错 5 倍的根因."""
    today = date(2026, 11, 11)
    # 一天 30 条，另一天 5 条
    dates = ["2026-11-05"] * 30 + ["2026-11-11"] * 5
    r = viz.cadence(_c(dates), today=today)
    assert r["total"] == 35, r
    assert r["active_days"] == 2


def test_cadence_density_and_span():
    today = date(2026, 11, 11)
    # 10 天跨度里动了 5 天
    dates = ["2026-11-01", "2026-11-03", "2026-11-05", "2026-11-07", "2026-11-11"]
    r = viz.cadence(_c(dates), today=today)
    assert r["active_days"] == 5
    assert r["span_days"] == 10
    assert r["density"] == 50.0


def test_cadence_median_gap_ignores_outliers():
    """中位间隔要抗极端值：一天 50 条不该把"隔几天提交一次"算成"每天"."""
    today = date(2026, 11, 11)
    dates = ["2026-11-01"] * 50 + ["2026-11-05", "2026-11-09", "2026-11-11"]
    r = viz.cadence(_c(dates), today=today)
    assert r["median_gap"] == 4, r         # 11-01→05→09→11 的间隔是 4/4/2，中位 4
    assert r["active_days"] == 4


def test_cadence_reports_stopped_project():
    today = date(2026, 11, 11)
    r = viz.cadence(_c(["2026-08-01", "2026-08-02"]), today=today)
    assert r["mode"] == "停了"
    assert r["idle_days"] == (today - date(2026, 8, 2)).days


def test_cadence_empty_commits():
    r = viz.cadence([], today=date(2026, 11, 11))
    assert r["empty"] is True
    assert r["total"] == 0


def test_cadence_ignores_bad_dates():
    r = viz.cadence([{"date": ""}, {"date": "不是日期"}, {}], today=date(2026, 11, 11))
    assert r["empty"] is True
