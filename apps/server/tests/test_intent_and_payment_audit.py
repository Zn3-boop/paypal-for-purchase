"""统一入口 + 支付留痕回归 —— 覆盖三件事：

1. `POST /plans/intent` 是单人/团购**共用**的入口：原句就是 Channel3 检索关键词，
   并在 `GET /plans` 的 `retrieval` / `intent_query` / `intent_mode` 原样回显；
2. 方案对比必须是「同池逐件升级」：`shared_count > 0`，且 `upgrades` 里没有
   「同一件商品 from == to」这种自相矛盾的条目；
3. 支付 Agent 失败也要留痕：审计时间轴出现 `payment_failed`，订单档案出现
   `cancelled` 记录；重复回调不会重复记账（幂等）。

前两条是 HTTP 级（真实 TestClient），第三条是函数级（避免依赖 PayPal 网络）。
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.channel3 import Channel3Result


def _result(items):
    """把 mock 商品列表包成三态结果：空列表 = empty（检索成功但无匹配）。"""
    return Channel3Result(list(items), "ok" if items else "empty", "")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _reset(client, sid: str) -> None:
    r = client.post(
        "/api/v1/session/reset",
        json={"session_id": sid, "user_id": "intent-tester", "hard_reset": True},
    )
    assert r.status_code == 200, r.text


def _audit_events(client, sid: str) -> list[str]:
    r = client.get("/api/v1/audit/logs", params={"session_id": sid, "limit": 500})
    assert r.status_code == 200, r.text
    return [e["event"] for e in r.json()["logs"]]


@pytest.fixture
def channel3_stub(monkeypatch):
    """用确定性假数据替掉真实 Channel3，避免测试依赖外网 / API key 抖动。

    构造刻意保留一个「单件品类」：B 的同品类升级没有可替换的同伴，
    该 slot 必然原样保留 —— 于是 shared_count >= 1 由数据保证，
    而不是靠"外部搜索恰好可用"。这样全量跑与单文件跑结果一致。
    """
    items = [
        # 同品类 3 件：可以发生「同品类升级」（from != to，delta > 0）
        {"id": "ch3_board_1", "name": "环保桌游·森林棋", "category": "桌游", "price": 25.0,
         "currency": "USD", "tags": ["桌游", "环保", "奖品"], "source": "channel3", "image_url": ""},
        {"id": "ch3_board_2", "name": "环保桌游·生态大富翁", "category": "桌游", "price": 30.0,
         "currency": "USD", "tags": ["桌游", "环保", "大富翁"], "source": "channel3", "image_url": ""},
        {"id": "ch3_board_3", "name": "环保桌游·垃圾分类挑战", "category": "桌游", "price": 38.0,
         "currency": "USD", "tags": ["桌游", "环保", "垃圾分类"], "source": "channel3", "image_url": ""},
        {"id": "ch3_pad_1", "name": "再生纸便签本", "category": "文具", "price": 5.0,
         "currency": "USD", "tags": ["文具", "再生纸"], "source": "channel3", "image_url": ""},
        {"id": "ch3_pad_2", "name": "竹纤维签字笔", "category": "文具", "price": 8.0,
         "currency": "USD", "tags": ["文具", "竹纤维"], "source": "channel3", "image_url": ""},
        # 单件品类：没有同伴可换，B 必然保留 → 共有商品真实存在
        {"id": "ch3_bag_1", "name": "有机棉环保帆布袋", "category": "包装", "price": 15.0,
         "currency": "USD", "tags": ["环保", "帆布袋"], "source": "channel3", "image_url": ""},
    ]

    async def _fake_search(query: str, limit: int = 20):  # noqa: ARG001 - 与真实签名一致
        return [dict(it) for it in items]

    async def _fake_search_status(query: str, limit: int = 20):  # noqa: ARG001
        return _result([dict(it) for it in items])

    monkeypatch.setattr(
        "app.services.channel3.search_products_for_query", _fake_search
    )
    # planner 走三态入口；两个断点都打上，避免测试真触网。
    monkeypatch.setattr(
        "app.services.channel3.search_products_with_status", _fake_search_status
    )
    yield items


# ---------------------------------------------------------------- 统一入口

def test_intent_is_single_entry_for_single_and_group(client):
    """同一个提问框：团购措辞 → group；单人措辞 → single。"""
    sid = _sid("intent-mode")
    _reset(client, sid)

    r = client.post(
        "/api/v1/plans/intent",
        params={"session_id": sid},
        json={"message": "帮我找 5 人份的环保桌游奖品，预算 300 美元"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["mode"] == "group"
    # 关键词必须逐字保留（不二次改写），这是「推荐依据」的原始证据
    assert body["query"] == "帮我找 5 人份的环保桌游奖品，预算 300 美元"

    # 预算要认「300」而不是「5 人份」里的 5
    constraints = client.get(
        "/api/v1/constraints", params={"session_id": sid}
    ).json()
    budgets = [c for c in constraints["constraints"] if c["type"] == "budget"]
    assert budgets and budgets[0]["numeric_value"] == 300.0, constraints
    assert "intent_submitted" in _audit_events(client, sid)

    sid2 = _sid("intent-single")
    _reset(client, sid2)
    r = client.post(
        "/api/v1/plans/intent",
        params={"session_id": sid2},
        json={"message": "我要一张 R&B 黑胶唱片"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "single"


def test_plans_echo_retrieval_and_query(client, channel3_stub):
    """GET /plans 必须回显「按哪个关键词检索、命中多少」，而不是只给一堆商品。"""
    sid = _sid("intent-echo")
    _reset(client, sid)
    query = "帮我找 5 人份的环保桌游奖品，预算 300 美元"
    r = client.post(
        "/api/v1/plans/intent", params={"session_id": sid}, json={"message": query}
    )
    assert r.status_code == 200, r.text

    payload = client.get("/api/v1/plans", params={"session_id": sid}).json()
    assert payload["intent_query"] == query
    assert payload["intent_mode"] == "group"
    retrieval = payload["retrieval"]
    assert retrieval["query"] == query
    assert retrieval["source"] == "channel3"
    # 命中漏斗：候选池必须来自 stub（同池、不超过 stub 总数），过滤后仍 ≥ 1
    assert 1 <= retrieval["candidate_count"] <= len(channel3_stub), retrieval
    assert retrieval["candidate_count"] >= retrieval["matched_count"] >= 1
    assert retrieval["channel3_status"] == "ok", retrieval
    # 方案里的商品必须真正来自 stub（诚实性：不存在"另一个宇宙"的货）
    stub_ids = {it["id"] for it in channel3_stub}
    all_item_ids = {
        it["id"] for p in payload["plans"] for it in p["items"]
    }
    assert all_item_ids and all_item_ids <= stub_ids


# ---------------------------------------------------------------- 同池升级

def test_comparison_shares_items_and_has_no_noop_upgrades(client, channel3_stub):
    """B 是 A 的同品类升级：共有商品真实存在，upgrades 不含 0 价差同款。"""
    sid = _sid("intent-shared")
    _reset(client, sid)
    client.post(
        "/api/v1/plans/intent",
        params={"session_id": sid},
        json={"message": "帮我找 5 人份的环保桌游奖品，预算 300 美元"},
    )

    payload = client.get("/api/v1/plans", params={"session_id": sid}).json()
    assert payload["data_source"] == "channel3"
    plans = {p["plan_id"]: p for p in payload["plans"]}
    # 两方案来自同一候选池：所有商品 id 都能在 stub 里找到（不存在"另一个宇宙"的货）
    stub_ids = {it["id"] for it in channel3_stub}
    assert {it["id"] for it in plans["A"]["items"]} <= stub_ids
    assert {it["id"] for it in plans["B"]["items"]} <= stub_ids

    comparison = payload["comparison"]
    assert comparison["shared_count"] >= 1, comparison
    # 恒等式：shared = |A| - only_A = |B| - only_B（数字必须自洽）
    assert comparison["shared_count"] == (
        comparison["item_count_a"] - comparison["only_a_count"]
    )
    assert comparison["shared_count"] == (
        comparison["item_count_b"] - comparison["only_b_count"]
    )
    # 逐件升级不得出现「同价换同价」的 0 价差噪声
    for up in comparison["upgrades"]:
        assert up["delta"] != 0, up
        assert up["delta"] == round(up["to"] - up["from"], 2), up


# ---------------------------------------------------------------- 支付留痕

def test_agent_payment_failure_is_logged_and_idempotent():
    """建单/捕获失败也必须出现在审计时间轴与订单档案里（且不重复记账）。"""
    from app.api.payment import _PAYMENT_EVENT_DONE, _record_agent_activity
    from app.services.group_purchase.audit_log import get_audit_logger
    from app.services.group_purchase.order_lifecycle import (
        OrderStatus,
        list_order_records,
    )

    _PAYMENT_EVENT_DONE.clear()
    sid = _sid("payfail")
    state = {
        "paypal_status": "capture_failed",
        "paypal_order_id": "PAYPAL-FAIL-001",
        "paypal_mode": "sandbox_real",
        "paypal_source": "agent_card",
        "paypal_msg": "INSTRUMENT_DECLINED：卡被发卡行拒绝",
        "shopping_selected": {
            "id": "k1_004",
            "name": "环保帆布袋",
            "price": 12.5,
            "currency": "USD",
            "category": "包装",
            "image_url": "https://example.com/bag.png",
        },
    }

    _record_agent_activity(state, session_id=sid, user_id="buyer@example.com")
    # 重放（PayPal 回调重试）：不得重复记账
    _record_agent_activity(state, session_id=sid, user_id="buyer@example.com")

    entries = get_audit_logger().get_session_timeline(sid)
    failed = [e for e in entries if e.event == "payment_failed"]
    assert len(failed) == 1, [e.event for e in entries]
    data = failed[0].data
    assert data["paypal_order_id"] == "PAYPAL-FAIL-001"
    assert data["reason"].startswith("INSTRUMENT_DECLINED")
    assert data["amount"] == 12.5
    assert data["mode"] == "sandbox_real"

    orders = list_order_records(session_id=sid)
    assert orders, "失败尝试也必须有订单档案记录"
    assert orders[0].status == OrderStatus.CANCELLED
    assert orders[0].cancelled_reason.startswith("INSTRUMENT_DECLINED")
    assert orders[0].paypal_token == "PAYPAL-FAIL-001"
    # 审计事件与档案订单号对得上（时间轴点得进档案）
    assert data["order_id"] == orders[0].order_id
    assert state["business_order_id"] == orders[0].order_id


def test_agent_payment_created_logs_started_and_paying_order():
    """建单成功：payment_started 留痕，档案进入 paying（时间轴与档案同一单号）。"""
    from app.api.payment import _PAYMENT_EVENT_DONE, _record_agent_activity
    from app.services.group_purchase.audit_log import get_audit_logger
    from app.services.group_purchase.order_lifecycle import (
        OrderStatus,
        list_order_records,
    )

    _PAYMENT_EVENT_DONE.clear()
    sid = _sid("paystart")
    state = {
        "paypal_status": "created",
        "paypal_order_id": "PAYPAL-OK-001",
        "paypal_mode": "mock",
        "paypal_source": "agent_card",
        "shopping_selected": {
            "id": "k1_010",
            "name": "R&B 黑胶唱片",
            "price": 24.0,
            "currency": "USD",
        },
    }
    _record_agent_activity(state, session_id=sid, user_id="buyer@example.com")

    events = [e.event for e in get_audit_logger().get_session_timeline(sid)]
    assert "payment_started" in events
    orders = list_order_records(session_id=sid)
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.PAYING
    assert orders[0].paypal_token == "PAYPAL-OK-001"