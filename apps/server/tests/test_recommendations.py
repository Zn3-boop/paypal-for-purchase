"""Channel3 推荐回归 —— 有出处、可复算、不越护栏。

契约：
  - 没有历史信号 → 直接 `no_signal`，不编造推荐；
  - 有信号 → 真打 Channel3，每张卡都带 basis / reason / query_used，且 `search_hits` 是
    真实命中条数（评委拿 query_used 自己能复算）；
  - Channel3 不可用 → 空列表 + disabled/error，绝不用本地目录冒充；
  - 已在购物车/已选过的商品不再推荐；
  - 加购必须先用过期缓存校验，超硬预算默认 **blocked 且购物车不变**，force 才放行。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

import pytest

from app.services.channel3 import Channel3Result
from app.services.group_purchase import recommendations as rec
from app.services.group_purchase import search_memory as sm
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.session_state import (
    PlanItem,
    ShoppingPlan,
    get_or_create_session,
    get_session,
    update_session,
)


@pytest.fixture(autouse=True)
def _clear_recommendation_cache():
    """推荐缓存是进程级的；每个测试都要从空缓存起步，否则会读到上个测试的卡片。"""
    rec._cache.clear()
    yield
    rec._cache.clear()

QUERY = "jazz vinyl gift"

FAKE_ITEMS = [
    {
        "id": "ch3_1",
        "name": "Jazz Vinyl Record Box Set",
        "category": "music",
        "price": 39.9,
        "tags": ["jazz", "vinyl"],
        "match_score": 2,
    },
    {
        "id": "ch3_2",
        "name": "Vinyl Record Cleaning Kit",
        "category": "music",
        "price": 14.5,
        "tags": ["vinyl", "care"],
        "match_score": 1,
    },
    {
        "id": "ch3_3",
        "name": "Garden Hose Nozzle",
        "category": "garden",
        "price": 8.0,
        "tags": ["garden"],
        "match_score": 0,
    },
]


def _fake_search(items=None, status="ok", message=""):
    payload = [dict(i) for i in (FAKE_ITEMS if items is None else items)]

    async def _inner(query: str, limit: int = 8) -> Channel3Result:
        return Channel3Result(items=[dict(i) for i in payload], status=status, message=message)

    return _inner


def _session(session_id="sess-rec", *, budget=None, cart=None, query=QUERY):
    session = get_or_create_session(session_id, "user-rec")
    session.intent_query = query
    if budget is not None:
        session.constraints = [
            Constraint(
                id="c-budget",
                type="budget",
                content=f"总预算 {budget}",
                hard=True,
                source="成员 A",
                numeric_value=float(budget),
            )
        ]
    if cart:
        session.cart_items = list(cart)
    update_session(session)
    return session


def test_no_history_returns_no_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))

    out = asyncio.run(rec.build_recommendations(None, limit=3))

    assert out["items"] == []
    assert out["source"] == "none"
    assert out["channel3"]["status"] == "no_signal"
    assert "历史信号" in out["note"]


def test_every_card_cites_its_source_and_hit_count(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("sess-rec", "user-rec", QUERY)
    session = _session()
    monkeypatch.setattr("app.services.channel3.search_products_with_status", _fake_search())

    out = asyncio.run(rec.build_recommendations(session, limit=3))

    assert out["source"] == "channel3"
    assert out["query_used"] == QUERY
    assert out["search_hits"] == len(FAKE_ITEMS)
    assert out["shown"] == len(out["items"]) > 0
    for card in out["items"]:
        assert card["source"] == "channel3"
        assert card["basis"] in {"search", "selection", "anchor"}
        assert card["reason"]
        assert card["price"] > 0

    # 出处必须能对上「你搜过的原句」，而不是一段装饰文字
    assert any("jazz vinyl gift" in c["reason"] for c in out["items"])
    # 不相关的园艺术品被相关性闸门挡掉
    assert all("Garden" not in c["name"] for c in out["items"])


def test_channel3_disabled_returns_honest_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("sess-rec", "user-rec", QUERY)
    session = _session()
    monkeypatch.setattr(
        "app.services.channel3.search_products_with_status",
        _fake_search(items=[], status="disabled", message="未配置 CHANNEL3_API_KEY"),
    )

    out = asyncio.run(rec.build_recommendations(session, limit=3))

    assert out["items"] == []
    assert out["source"] == "none"
    assert out["channel3"]["status"] == "disabled"
    assert "不可用" in out["note"]


def test_items_already_in_cart_are_not_recommended(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("sess-rec", "user-rec", QUERY)
    in_cart = PlanItem(id="i-cart", name="Jazz Vinyl Record Box Set", category="music", price=39.9)
    session = _session(cart=[in_cart])
    monkeypatch.setattr("app.services.channel3.search_products_with_status", _fake_search())

    out = asyncio.run(rec.build_recommendations(session, limit=3))

    assert all("Jazz Vinyl Record Box Set" != c["name"] for c in out["items"])


def test_add_requires_a_fresh_recommendation(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    _session()

    out = rec.add_recommendation_to_cart("sess-rec", "rec-1")

    assert out["success"] is False
    assert out["code"] == "stale_recommendation"


def test_add_appends_item_to_cart(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("sess-rec", "user-rec", QUERY)
    session = _session(cart=[PlanItem(id="i0", name="Speaker Stand", category="audio", price=12.0)])
    monkeypatch.setattr("app.services.channel3.search_products_with_status", _fake_search())

    cards = asyncio.run(rec.build_recommendations(session, limit=3))
    card = cards["items"][0]
    out = rec.add_recommendation_to_cart("sess-rec", card["id"])

    assert out["success"] is True
    assert out["item"]["source"] == "channel3"
    assert out["item"]["name"] == card["name"]
    assert out["cart"]["total"] == round(12.0 + card["price"], 2)
    assert len(out["cart"]["items"]) == 2
    assert len(get_session("sess-rec").cart_items) == 2


def test_add_is_blocked_by_hard_budget_and_leaves_cart_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("sess-rec", "user-rec", QUERY)
    session = _session(
        budget=50.0,
        cart=[PlanItem(id="i0", name="Speaker Stand", category="audio", price=40.0)],
    )
    monkeypatch.setattr("app.services.channel3.search_products_with_status", _fake_search())

    cards = asyncio.run(rec.build_recommendations(session, limit=3))
    out = rec.add_recommendation_to_cart("sess-rec", cards["items"][0]["id"])

    assert out["success"] is False
    assert out["code"] == "budget_blocked"
    assert out["guardrail"]["status"] == "blocked"
    assert out["guardrail"]["overage"] > 0
    # 关键：被拦时购物车必须原样不动，不能先加后拦
    assert out["cart"]["total"] == 40.0
    assert len(out["cart"]["items"]) == 1
    assert len(get_session("sess-rec").cart_items) == 1


def test_force_bypasses_budget_block(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("sess-rec", "user-rec", QUERY)
    session = _session(
        budget=50.0,
        cart=[PlanItem(id="i0", name="Speaker Stand", category="audio", price=40.0)],
    )
    monkeypatch.setattr("app.services.channel3.search_products_with_status", _fake_search())

    cards = asyncio.run(rec.build_recommendations(session, limit=3))
    out = rec.add_recommendation_to_cart("sess-rec", cards["items"][0]["id"], force=True)

    assert out["success"] is True
    assert out["guardrail"]["status"] == "fail"
    assert len(out["cart"]["items"]) == 2


def _plan(plan_id: str, items: list[PlanItem]) -> ShoppingPlan:
    return ShoppingPlan(
        plan_id=plan_id,
        strategy="test",
        items=list(items),
        total=round(sum(i.price * i.quantity for i in items), 2),
    )


def test_placement_says_which_plan_already_has_the_card(tmp_path, monkeypatch):
    """每张卡都要回答「它和上方 A/B 是什么关系」，这是推荐与对比的结合点。"""
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("sess-place-a", "user-rec", QUERY)
    # 独立 session id：会话字典是进程级的，复用 id 会读到别的测试留下的购物车/方案
    session = _session("sess-place-a")
    session.plan_a = _plan(
        "A", [PlanItem(id="c1", name="Vinyl Record Cleaning Kit", category="music", price=14.5)]
    )
    session.plan_b = _plan(
        "B", [PlanItem(id="c2", name="Jazz Vinyl Record Box Set", category="music", price=39.9)]
    )
    update_session(session)
    monkeypatch.setattr("app.services.channel3.search_products_with_status", _fake_search())

    out = asyncio.run(rec.build_recommendations(session, limit=3))
    by_name = {c["name"]: c for c in out["items"]}

    assert by_name["Jazz Vinyl Record Box Set"]["placement"] == "in_b"
    assert by_name["Vinyl Record Cleaning Kit"]["placement"] == "in_a"
    assert by_name["Jazz Vinyl Record Box Set"]["placement_reason"]


def test_placement_outside_both_plans_states_the_rule_that_excluded_it(tmp_path, monkeypatch):
    """「两方案都没有」不能是裸标签：必须给可复核的排除原因（这里是被预算排除）。"""
    monkeypatch.setenv("SEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    sm.record_search("sess-place-b", "user-rec", QUERY)
    session = _session("sess-place-b", budget=45.0)
    # 方案里没有 music 品类，所以不会走「同品类已有更便宜的」分支；只剩预算这一条能排除它
    session.plan_a = _plan(
        "A", [PlanItem(id="c3", name="Speaker Stand", category="audio", price=40.0)]
    )
    session.plan_b = _plan(
        "B", [PlanItem(id="c4", name="Speaker Stand Pro", category="audio", price=42.0)]
    )
    update_session(session)
    monkeypatch.setattr("app.services.channel3.search_products_with_status", _fake_search())

    out = asyncio.run(rec.build_recommendations(session, limit=3))
    card = {c["name"]: c for c in out["items"]}["Jazz Vinyl Record Box Set"]

    assert card["placement"] == "neither"
    assert "超预算" in card["placement_reason"]
    assert "39.90" in card["placement_reason"]

