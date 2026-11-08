"""Channel3 检索记忆回归 —— 搜过 / 选过的事实必须落盘、去重、可一键复现。

契约：
  - record_search：空串不记；同一句（规范化后）在 recent_searches 里只出现一次并带 count；
  - record_selection：记录方案号 / 总额 / 件数 / 当时的检索词，最近一次排最前；
  - 读取每次都从 JSONL 重建（重启后仍在），损坏行跳过；
  - 记忆**不参与**检索：没有任何函数会把它拼回 Channel3 查询（本文件只验证只读事实）。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase import search_memory as sm


def test_record_search_dedupes_and_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))

    assert sm.record_search("s1", "u1", "  ") is None  # 空串不记
    sm.record_search("s1", "u1", "社团采购 户外 装饰", mode="group")
    sm.record_search("s2", "u1", "社团采购   户外 装饰", mode="group")  # 空格不同、视为同一句
    sm.record_search("s3", "u1", "给 5 人买饮用水", mode="single")

    recent = sm.recent_searches(limit=8)
    assert len(recent) == 2
    latest = recent[0]  # 时间倒序：最后一条是饮用水
    assert latest["query"] == "给 5 人买饮用水"
    deduped = next(r for r in recent if "装饰" in r["query"])
    assert deduped["count"] == 2


def test_selection_memory_order_and_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))

    sm.record_selection("s1", "u1", "A", 26.0, ["气球", "彩带"], query="社团采购 户外")
    sm.record_selection("s1", "u1", "B", 393.0, ["帐篷"], query="社团采购 户外")

    selections = sm.recent_selections(limit=5)
    assert [s["plan_id"] for s in selections] == ["B", "A"]
    assert selections[0]["total"] == 393.0
    assert selections[0]["item_count"] == 1
    assert selections[0]["query"] == "社团采购 户外"


def test_memory_survives_corrupted_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("s1", "u1", "预算 600 的装饰")

    path = sm.get_searches_file()
    with open(path, "a", encoding="utf-8") as f:
        f.write("{broken\n")

    recent = sm.recent_searches(limit=5)
    assert [r["query"] for r in recent] == ["预算 600 的装饰"]


def test_search_memory_endpoint_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("s1", "u1", "社团采购 户外")
    sm.record_selection("s1", "u1", "A", 26.0, ["气球"], query="社团采购 户外")

    from app.api.search_memory import get_search_memory

    payload = asyncio.run(get_search_memory(limit=5, selections_limit=3))
    assert payload["recent_searches"][0]["query"] == "社团采购 户外"
    assert payload["recent_selections"][0]["plan_id"] == "A"
