"""P1-3 回归：Channel3 429 限流显式降级（与熔断解耦，2026-11-15）。

覆盖：
  R1 真实请求命中 429 → reason_code=rate_limited，且**不打开** 60s 故障熔断
  R2 429 后冷却期内再次请求 → 不发网络请求，直接 reason_code=limit_cooldown
  R3 冷却期内 force_probe → 穿透，放行一次真实请求
  R4 冷却期结束 → 自动恢复真实请求（无需半开探针）
  R5 真实请求成功 → 同时清空限流冷却与熔断
  R6 配置关闭限流降级 → 429 走原普通错误路径（打开熔断）
  R7 _extract_status_code 覆盖 httpx.HTTPStatusError / 裸 status_code / 无码
  R8 taste_select_node：限流降级写审计事件 channel3_rate_limited

核心语义：429 =「上游忙、配额满」≠ 故障（5xx/网络/超时）。限流走独立短冷却，
不打开熔断 —— 否则一个瞬时限流会把 60s 内所有请求打进本地目录（过度降级）。
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")

from app.services.channel3 import client as ch3  # noqa: E402
from app.services.channel3.client import (  # noqa: E402
    CHANNEL3_EMPTY,
    CHANNEL3_ERROR,
    CHANNEL3_OK,
    Channel3Config,
    Channel3Result,
    Channel3SearchClient,
)
from app.services.payment_agent.nodes import taste_select_node  # noqa: E402

_KEY = "test-key-429"


def _make_client(**kwargs: object) -> Channel3SearchClient:
    cfg = Channel3Config(
        api_key=_KEY,
        base_url="",
        timeout_seconds=5.0,
        connect_timeout_seconds=3.0,
        max_retries=1,
        **kwargs,  # type: ignore[arg-type]
    )
    return Channel3SearchClient(config=cfg)


def _http_429_exc() -> BaseException:
    import httpx

    request = httpx.Request("GET", "https://api.trychannel3.com/v1/products")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("Too Many Requests", request=request, response=response)


@pytest.fixture(autouse=True)
def _reset_ch3_state():
    """每个用例前后清空熔断与限流冷却，避免跨用例污染。"""
    ch3._reset_circuit_breaker()
    ch3._reset_rate_limit()
    yield
    ch3._reset_circuit_breaker()
    ch3._reset_rate_limit()


# ---------------------------------------------------------------------------
# R1：429 → rate_limited，不打开熔断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r1_429_maps_to_rate_limited_not_circuit() -> None:
    client = _make_client()
    with patch.object(
        client, "_search_sync", side_effect=_http_429_exc()
    ):
        result = await client.search_products_with_status("water bottle")

    assert result.status == CHANNEL3_ERROR
    assert result.reason_code == "rate_limited"
    assert "429" in result.message
    # 关键：429 是限流不是故障 → 熔断必须保持关闭（否则 60s 内全短路）
    assert not ch3._circuit_open()
    # 但限流冷却已生效
    assert ch3._rate_limit_open(cooldown=ch3._RATE_LIMIT_COOLDOWN_SECONDS)


# ---------------------------------------------------------------------------
# R2：冷却期内再次请求 → 不发网络请求，直接 limit_cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_cooldown_skips_network() -> None:
    client = _make_client()
    with patch.object(
        client, "_search_sync", side_effect=_http_429_exc()
    ) as mocked:
        first = await client.search_products_with_status("water bottle")
        second = await client.search_products_with_status("water bottle")

    assert first.reason_code == "rate_limited"
    assert second.reason_code == "limit_cooldown"
    # 冷却期内只发过一次真实请求（第二次短路，未触网）
    assert mocked.call_count == 1


# ---------------------------------------------------------------------------
# R3：force_probe 穿透冷却（与熔断半开探针同语义）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r3_force_probe_pierces_cooldown() -> None:
    client = _make_client()
    with patch.object(client, "_search_sync", side_effect=_http_429_exc()) as mocked:
        await client.search_products_with_status("water bottle")  # 触发 429 → 冷却
        assert mocked.call_count == 1
        # 用户显式重试（探针）→ 穿透冷却，放行一次真实请求（仍会 429）
        third = await client.search_products_with_status(
            "water bottle", force_probe=True
        )

    assert third.reason_code == "rate_limited"  # 探针真实请求也还是 429
    assert mocked.call_count == 2  # 探针穿透了冷却，真发了请求


# ---------------------------------------------------------------------------
# R4：冷却期结束自动恢复（无需探针）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r4_cooldown_expires_and_recovers() -> None:
    client = _make_client()
    with patch.object(client, "_search_sync", side_effect=_http_429_exc()):
        await client.search_products_with_status("water bottle")

    # 手动把冷却起点拨回过去 → 模拟 30s 窗口流逝
    ch3._rate_limited_at -= 1000.0

    with patch.object(
        client,
        "_search_sync",
        return_value=[],  # 恢复后真实请求成功（空结果也算成功）
    ) as mocked:
        recovered = await client.search_products_with_status("water bottle")

    assert recovered.status == CHANNEL3_EMPTY  # 真实请求空结果 → 成功路径
    assert not recovered.reason_code
    assert mocked.call_count == 1  # 冷却结束后自动重新发请求
    # 成功路径应清空限流冷却
# ---------------------------------------------------------------------------
# R5：成功请求清空限流冷却与熔断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r5_success_resets_both_buckets() -> None:
    client = _make_client()

    with patch.object(client, "_search_sync", side_effect=_http_429_exc()):
        await client.search_products_with_status("water bottle")
    assert ch3._circuit_open() is False  # 429 不该开熔断
    assert ch3._rate_limited_at is not None

    # 下一次真实请求成功（返回一个商品）
    product = {"id": "ch3_t", "name": "Thermos", "category": "Bottles",
               "price": 19.9, "currency": "USD", "tags": ["stainless"]}
    with patch.object(
        client, "_search_sync", return_value=[product]
    ) as mocked:
        ok = await client.search_products_with_status("water bottle", force_probe=True)

    assert ok.status == CHANNEL3_OK
    assert mocked.call_count == 1
    assert ch3._rate_limited_at is None  # 冷却已复位
    assert not ch3._rate_limit_open(cooldown=1.0)


# ---------------------------------------------------------------------------
# R6：配置关闭限流降级 → 429 走原普通错误路径（打开熔断）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r6_degrade_disabled_uses_circuit_breaker() -> None:
    client = _make_client(rate_limit_degrade_enabled=False)
    with patch.object(client, "_search_sync", side_effect=_http_429_exc()):
        result = await client.search_products_with_status("water bottle")

    assert result.status == CHANNEL3_ERROR
    assert result.reason_code == ""  # 未细分
    assert "请求失败" in result.message  # 普通错误文案
    assert ch3._circuit_open() is True  # 走熔断（老行为）


# ---------------------------------------------------------------------------
# R7：_extract_status_code 覆盖三种形态
# ---------------------------------------------------------------------------


def test_r7_extract_status_code_variants() -> None:
    # httpx.HTTPStatusError.response.status_code
    assert ch3._extract_status_code(_http_429_exc()) == 429
    # 裸 status_code 属性
    class Bare:
        status_code = 503

    assert ch3._extract_status_code(Bare()) == 503
    # 无任何状态码
    assert ch3._extract_status_code(ValueError("boom")) is None
    # response 存在但无 status_code
    class Odd:
        response = object()

    assert ch3._extract_status_code(Odd()) is None


# ---------------------------------------------------------------------------
# R8：taste_select_node 限流降级写审计（channel3_rate_limited）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r8_taste_select_writes_rate_limit_audit(tmp_path) -> None:
    from app.services.group_purchase.audit_log import get_audit_logger

    state = {
        "session_id": "s-429r8",
        "user_id": "u-429r8",
        "shopping_intent": {
            "category": "bottles",
            "budget": 50,
            "keywords": ["water bottle"],
        },
        "monthly_spent": 10.0,
    }

    def _fake_search(query, limit=8, force_probe=False):
        return Channel3Result([], CHANNEL3_ERROR, "429", reason_code="rate_limited")

    with patch(
        "app.services.channel3.search_products_with_status",
        new=AsyncMock(side_effect=_fake_search),
    ):
        out = await taste_select_node(state)

    assert out["shopping_rank_source"] == "local_ranker"  # 降级本地
    logs = get_audit_logger().get_logs(session_id="s-429r8", event_type="channel3_rate_limited")
    assert logs, "限流降级必须留审计事件"
    assert logs[-1].data["reason_code"] == "rate_limited"
    assert logs[-1].data["fallback"] == "local_ranker"


@pytest.mark.asyncio
async def test_r8b_cooldown_degrade_also_audited(tmp_path) -> None:
    """冷却期内再次降级（limit_cooldown）也要留痕——用户看到的是第二次降级。"""
    from app.services.group_purchase.audit_log import get_audit_logger

    state = {
        "session_id": "s-429r8b",
        "user_id": "u-429r8b",
        "shopping_intent": {"category": "bottles", "budget": 50, "keywords": ["k"]},
        "monthly_spent": 10.0,
    }

    # 用真实调用点验证审计：taste_select 调用的 search_products_with_status
    # 在冷却期内返回 limit_cooldown 降级
    with patch(
        "app.services.channel3.search_products_with_status",
        new=AsyncMock(return_value=Channel3Result(
            [], CHANNEL3_ERROR, "cooldown", reason_code="limit_cooldown")),
    ):
        out = await taste_select_node(state)

    assert out["shopping_rank_source"] == "local_ranker"
    logs = get_audit_logger().get_logs(
        session_id="s-429r8b", event_type="channel3_rate_limited")
    assert logs and logs[-1].data["reason_code"] == "limit_cooldown"
    assert not ch3._rate_limit_open(cooldown=1.0)