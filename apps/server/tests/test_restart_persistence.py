"""重启持久化回归 —— 「📜 审计时间轴」与「📦 订单档案」在 uvicorn 重启后不丢。

背景（真实问题，2026-11-08 定位）：
  - 审计日志一直按 JSONL 落盘（data/audit_logs/<user>/audit_<date>.jsonl），
    但 `get_logs(session_id=...)` 只命中进程内缓存；重启后缓存为空、
    又因为没有 user_id 而不会扫盘 → 磁盘有记录却返回空时间轴。
  - 订单档案 `_orders` 是纯内存注册表，重启即丢；启动对账也遍历不到任何订单。

新契约（本文件的回归目标）：
  1. audit：缓存未命中时扫描所有用户目录里匹配 session_id 的 JSONL，并回填缓存；
     时间轴保持「时间正序、最近 limit 条」语义（前端 `.slice().reverse()` 依赖它）。
  2. order：每次状态落盘追加一行完整记录；启动 `load_orders_from_disk()` 按
     「同一 order_id 最后一行」恢复，且 setdefault 不覆盖运行时订单。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.audit_log import AuditLogger
from app.services.group_purchase import order_lifecycle as ol


# ============================================================
# 1. 审计时间轴：重启后仍可查到
# ============================================================


def test_audit_timeline_survives_restart(tmp_path):
    store = str(tmp_path / "audit_logs")
    first = AuditLogger(store_dir=store)
    first.log(
        session_id="sess-restart",
        user_id="u1",
        actor="tester",
        event="plan_generated",
        data={"n": 1},
    )
    first.log(
        session_id="sess-restart",
        user_id="u1",
        actor="tester",
        event="review_confirmed",
        data={"n": 2},
    )
    # 另一个会话的日志不能被串进来
    first.log(
        session_id="sess-other",
        user_id="u1",
        actor="tester",
        event="plan_generated",
        data={"n": 3},
    )

    # 模拟重启：新实例，内存缓存为空
    fresh = AuditLogger(store_dir=store)
    timeline = fresh.get_session_timeline("sess-restart")

    assert [e.event for e in timeline] == ["plan_generated", "review_confirmed"]
    assert all(e.session_id == "sess-restart" for e in timeline)

    # 回填缓存后继续追加，顺序仍需保持正序
    fresh.log(
        session_id="sess-restart",
        user_id="u1",
        actor="tester",
        event="payment_completed",
        data={"n": 4},
    )
    assert [e.event for e in fresh.get_session_timeline("sess-restart")] == [
        "plan_generated",
        "review_confirmed",
        "payment_completed",
    ]


def test_audit_timeline_unknown_session_returns_empty(tmp_path):
    store = str(tmp_path / "audit_logs")
    logger = AuditLogger(store_dir=store)
    logger.log(
        session_id="sess-a",
        user_id="u1",
        actor="tester",
        event="plan_generated",
        data={},
    )
    fresh = AuditLogger(store_dir=store)
    assert fresh.get_session_timeline("sess-missing") == []


# ============================================================
# 2. 订单档案：重启后仍可恢复状态与转移轨迹
# ============================================================


def test_order_archive_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDER_STORE_DIR", str(tmp_path / "orders"))
    monkeypatch.setattr(ol, "_orders", {})
    # 审计写盘与本用例无关，替换为 no-op，避免污染仓库 data/ 目录
    monkeypatch.setattr(ol, "log_order_status_changed", lambda **kwargs: None)

    order = ol.create_order_record(
        session_id="sess-order",
        user_id="u1",
        total=26.0,
        items=[{"id": "i1", "name": "环保气球", "price": 26.0, "quantity": 1}],
        plan_id="A",
    )
    ok, msg = ol.cancel_order(order, actor="tester", reason="持久化用例")
    assert ok, msg

    # 模拟重启：清空内存注册表
    monkeypatch.setattr(ol, "_orders", {})
    restored = ol.load_orders_from_disk()

    assert restored == 1
    got = ol.get_order_record(order.order_id)
    assert got is not None
    assert got.session_id == "sess-order"
    assert got.status == ol.OrderStatus.CANCELLED
    assert got.total == 26.0
    assert got.cancelled_reason == "持久化用例"
    assert len(got.transitions) == 1
    assert got.transitions[0]["to"] == "cancelled"

    # 幂等：再加载不会产生重复记录
    assert ol.load_orders_from_disk() == 1
    assert len(ol.list_order_records(session_id="sess-order")) == 1


def test_order_store_skips_corrupted_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDER_STORE_DIR", str(tmp_path / "orders"))
    monkeypatch.setattr(ol, "_orders", {})
    monkeypatch.setattr(ol, "log_order_status_changed", lambda **kwargs: None)

    order = ol.create_order_record(
        session_id="sess-corrupt",
        user_id="u1",
        total=10.0,
        items=[],
    )
    path = ol.get_orders_file_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write("{not-json}\n")
        f.write(json.dumps({"order_id": "broken"}) + "\n")  # 缺 session_id

    monkeypatch.setattr(ol, "_orders", {})
    assert ol.load_orders_from_disk() == 1
    assert ol.get_order_record(order.order_id) is not None
