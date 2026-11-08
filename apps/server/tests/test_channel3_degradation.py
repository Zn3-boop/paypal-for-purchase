"""T1–T7 回归：Channel3 降级链 + LLM 诚实性（2026-10-28）。

语义（对齐新选品链路）：
  L1 = Channel3 真实商品搜索（有 CHANNEL3_API_KEY 且调用成功）→ rank_source="channel3"
  L2 = 本地 catalog + local_ranker 兜底（无 key / 空结果 / 异常 / 超时）→ rank_source="local_ranker"

覆盖：
  T1  无 key（降级）→ local_ranker、候选来自本地 catalog、shop_source 透出
  T2  L1 成功 → channel3、候选带 source="channel3"（真实商品来源标记）
  T3  L1 抛异常/超时 → 降级 L2，节点不硬失败（HTTP 层 200 不 500），日志记录降级原因
  T6  可解释性面板透出 rank_source / llm_source / source
  T7  LLM key 缺失/错误 → llm_source="mock_rule" 显式标注，禁止伪造 "real"
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")

from app.services.payment_agent.nodes import (  # noqa: E402
    _llm_write_authority_reason,
    authority_check_node,
    parse_intent_node,
    taste_select_node,
)

# 一条归一化后的 Channel3 商品样例（与 client._normalize_product 输出同构）
_CHANNEL3_ITEM = {
    "id": "ch3_demo1",
    "name": "Sony WH-1000XM4 Wireless Noise Cancelling Headphones",
    "category": "Over-Ear Headphones",
    "price": 298.0,
    "currency": "USD",
    "tags": ["noise cancelling", "Bluetooth", "Sony", "Over-Ear Headphones"],
    "description": "Industry-leading noise cancelling with 30-hour battery life.",
    "image_url": "https://cdn.trychannel3.com/demo.jpg",
    "product_url": "https://buy.trychannel3.com/demo",
    "source": "channel3",
    "raw_id": "demo1",
}

_BASE_STATE = {
    "raw_query": "帮我找一副降噪耳机，预算 300 以内",
    "user_id": "u-demo",
    "session_id": "s-demo",
    "monthly_spent": 10.0,
}


# 本地目录真实覆盖的品类（music）与目录外品类（耳机）两套状态：
# 前者验证"降级后仍有商品"，后者验证"降级也必须诚实收口，不许拿黑胶冒充耳机"。
_LOCAL_STATE = {
    "raw_query": "帮我找一张爵士黑胶唱片，预算 300 以内",
    "user_id": "u-demo",
    "session_id": "s-demo-local",
    "monthly_spent": 10.0,
}


async def _intent_state(base: dict | None = None) -> dict:
    state = dict(base or _BASE_STATE)
    r = parse_intent_node(state)
    if hasattr(r, "__await__"):
        r = await r
    return {**state, **r}


# ---------------------------------------------------------------------------
# T1：无 key → L2 本地兜底
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t1_no_key_falls_back_to_local_catalog():
    """T1: Channel3 不可用（无 key / 空结果）→ local_ranker + 本地 catalog 候选。"""
    state = await _intent_state(_LOCAL_STATE)  # 用目录真实覆盖的品类（music）

    # 强制 L1 空结果（等价于无 key / 搜索无命中）
    with patch(
        "app.services.channel3.search_products_for_query",
        new=AsyncMock(return_value=[]),
    ):
        out = await taste_select_node(state)

    assert out["shopping_rank_source"] == "local_ranker"
    assert out["shopping_selected"]["name"]
    # 本地目录商品没有 source 字段（不是 Channel3 来源）
    assert out["shopping_selected"].get("source") != "channel3"


@pytest.mark.asyncio
async def test_t1b_out_of_catalog_query_is_honestly_empty():
    """T1b: 降级也不能"抢答"——本地目录没有耳机时，宁可空选也不拿黑胶冒充。"""
    state = await _intent_state()  # _BASE_STATE = 降噪耳机（目录外品类）

    with patch(
        "app.services.channel3.search_products_for_query",
        new=AsyncMock(return_value=[]),
    ):
        out = await taste_select_node(state)

    assert out["shopping_rank_source"] == "local_ranker"
    assert out["shopping_selected"] == {}, "目录外品类不能有「选中商品」"
    assert out["shopping_error"] == "no_relevant_candidates"
    assert out["shopping_candidates"], "兜底提示仍需列出目录里真实可选的商品"


# ---------------------------------------------------------------------------
# T2：L1 成功 → channel3 + 真实来源标记
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t2_channel3_l1_success():
    """T2: 搜索返回真实商品 → rank_source=channel3，候选带 source=channel3。"""
    state = await _intent_state()

    with patch(
        "app.services.channel3.search_products_for_query",
        new=AsyncMock(return_value=[_CHANNEL3_ITEM]),
    ):
        out = await taste_select_node(state)

    assert out["shopping_rank_source"] == "channel3"
    selected = out["shopping_selected"]
    assert selected["name"]
    assert out["shopping_candidates"], "L1 命中后应有候选"
    assert all(c.get("source") == "channel3" for c in out["shopping_candidates"])
    assert out["shopping_anchors"], "锚点应包含 keywords"


# ---------------------------------------------------------------------------
# T3：L1 抛异常/超时 → 降级 L2，不硬失败，日志记录
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t3_channel3_exception_degrades_not_crash():
    """T3: 抛异常 → 节点仍返回 local_ranker 兜底（HTTP 200 不 500）+ 降级日志。"""
    state = await _intent_state(_LOCAL_STATE)

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("app.services.payment_agent.nodes")
    logger.addHandler(handler)
    try:
        with patch(
            "app.services.channel3.search_products_for_query",
            new=AsyncMock(side_effect=TimeoutError("channel3 timeout")),
        ):
            out = await taste_select_node(state)
    finally:
        logger.removeHandler(handler)

    assert out["shopping_rank_source"] == "local_ranker"
    assert out["shopping_selected"]["name"], "降级后仍须有商品输出"
    assert any("降级" in r.getMessage() or "Channel3" in r.getMessage() for r in records), (
        "应记录降级原因日志"
    )


# ---------------------------------------------------------------------------
# T6：可解释性面板透出 rank_source / llm_source / source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t6_panel_exposes_sources():
    """T6: assemble_explainability_panel 透出 rank_source / llm_source / payment.source。"""
    from app.services.payment_agent.panel import assemble_explainability_panel

    # 完整跑一遍 mock 链路，流出决策留痕
    state = await _intent_state()

    with patch(
        "app.services.channel3.search_products_for_query",
        new=AsyncMock(return_value=[_CHANNEL3_ITEM]),
    ):
        r = await taste_select_node(state)
    state = {**state, **r}

    r = await authority_check_node(state)
    state = {**state, **r, "paypal_mode": "mock", "paypal_source": "mock"}

    panel = assemble_explainability_panel(state)
    assert panel["rank_source"] == "channel3"
    assert panel["authority"]["llm_source"] in ("real", "mock_rule"), "llm_source 必须是本次调用结果"
    assert panel["item"]["name"], "面板应透出商品名"


# ---------------------------------------------------------------------------
# T7：LLM 失败 → mock_rule 显式标注（诚实性）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t7_llm_failure_is_mock_rule_not_real():
    """T7: LLM key 缺失/调用失败 → llm_source="mock_rule"，绝不伪造 "real"。"""
    with patch("app.services.payment_agent.nodes.LLM_MODE", "real"), patch(
        "app.services.llm.client.llm_parse_with_retry",
        new=AsyncMock(side_effect=RuntimeError("llm unavailable")),
    ):
        reason, source = await _llm_write_authority_reason(
            verdict="allow_auto",
            item={"name": "降噪耳机", "price": 298.0, "currency": "USD"},
            amount=298.0,
            trust_score=0.5,
            goal_conflicts=[],
            impulse_signals=[],
        )
    assert reason
    assert source == "mock_rule"
    assert source != "real"