"""Channel3 客户端韧性回归（2026-11-06）。

覆盖三件事（都是"演示不能卡、故障要能解释"的直接约束）：
  1. 证书校验失败必须**快速降级**：两套信任库都失败也要在 5s 内给出 error 三态，
     而不是把看板拖到"73 秒"；
  2. error **不写缓存**：一次网络抖动不能被钉 5 分钟，重试立刻生效；
     ok/empty 才缓存，同一 query（大小写/空白归一化后）录像时秒回；
  3. planner 把 channel3_status="error" 透出到 retrieval（UI 与审计都能读）。
"""
from __future__ import annotations

import os
import ssl
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")

from app.services.channel3 import (  # noqa: E402
    CHANNEL3_ERROR,
    CHANNEL3_OK,
    Channel3Result,
    Channel3SearchClient,
)
from app.services.channel3 import client as channel3_module  # noqa: E402
from app.services.channel3.config import Channel3Config  # noqa: E402
from app.services.group_purchase.planner import generate_dual_plans  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_channel3_circuit():
    """熔断状态是模块级全局：打开 60s 后，任何真实检索都会短路成 error。

    韧性测试恰恰需要「每次都真实走一遍失败路径」才能验证降级逻辑，
    所以每个用例前复位熔断，避免上一个用例的失败污染下一个。
    """
    channel3_module._reset_circuit_breaker()
    yield
    channel3_module._reset_circuit_breaker()


def _client() -> Channel3SearchClient:
    return Channel3SearchClient(
        Channel3Config(
            api_key="test-key",
            base_url="",
            timeout_seconds=5.0,
            connect_timeout_seconds=3.0,
            max_retries=1,
        )
    )


@pytest.mark.asyncio
async def test_cert_error_degrades_fast_and_reports_error():
    """两套信任库都证书失败 → 5s 内返回 error（不抛异常、不无限重试）。"""
    client = _client()
    attempts: list[int] = []

    def _boom(self, query, limit, index):  # noqa: ARG001 - 与真实签名一致
        attempts.append(index)
        raise ssl.SSLCertVerificationError(
            "certificate verify failed: unable to get local issuer certificate"
        )

    with patch.object(Channel3SearchClient, "_search_sync", _boom):
        started = time.monotonic()
        result = await client.search_products_with_status("jazz vinyl", 5)
        elapsed = time.monotonic() - started

    assert result.status == CHANNEL3_ERROR, result
    assert not result.ok
    assert "证书" in result.message, result.message
    assert elapsed < 5.0, f"证书失败必须快速降级，实际 {elapsed:.2f}s"
    # 每个信任来源各试一次（系统库 → certifi），不重复轰炸同一来源
    assert attempts == list(range(len(client._contexts)))


@pytest.mark.asyncio
async def test_hard_timeout_caps_hung_sdk_and_degrades(monkeypatch):
    """SDK 挂住时不能把 GET /plans 拖死：硬超时一到就返回 error，交给本地目录降级。

    现场真实故障：Channel3 网络抖动时，SDK 内部 10s 超时 × 重试 × 2 信任源，
    把方案生成拖到 90s+，UI 永远停在「⏳ 等待生成方案」。
    """
    client = _client()
    client._sdks = [None] * len(client._contexts)
    # 把硬超时压到 0.05s，让测试跑得快；SDK 侧模拟无限挂起
    monkeypatch.setattr(channel3_module, "_CHANNEL3_HARD_TIMEOUT", 0.05)

    def _hang(self, query, limit, index):  # noqa: ARG001
        time.sleep(1.0)
        return []

    with patch.object(Channel3SearchClient, "_search_sync", _hang):
        started = time.monotonic()
        result = await client.search_products_with_status("jazz vinyl", 5)
        elapsed = time.monotonic() - started

    assert result.status == CHANNEL3_ERROR, result
    assert not result.ok
    assert "超时" in result.message, result.message
    assert elapsed < 0.9, f"硬超时必须生效，实际 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_error_is_not_cached_so_retry_recovers():
    """error 不写缓存：第一次抖动失败，重试必须立刻拿到真实结果。"""
    channel3_module._CACHE.clear()
    calls = {"n": 0}

    async def _fake(self, query, limit=8):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return Channel3Result([], CHANNEL3_ERROR, "boom")
        return Channel3Result([{"id": "ch3_1", "name": "Sony WH-1000XM4"}], CHANNEL3_OK, "")

    with patch.object(Channel3SearchClient, "search_products_with_status", _fake):
        first = await channel3_module.search_products_with_status("retry me", 3)
        second = await channel3_module.search_products_with_status("retry me", 3)

    assert first.status == CHANNEL3_ERROR
    assert second.status == CHANNEL3_OK and second.items
    assert calls["n"] == 2, "error 命中缓存会让重试失效"


@pytest.mark.asyncio
async def test_ok_result_cached_and_key_normalized():
    """ok 结果入缓存，且大小写/多余空白归一化后命中（录制回放秒回）。"""
    channel3_module._CACHE.clear()
    calls = {"n": 0}

    async def _fake(self, query, limit=8):  # noqa: ARG001
        calls["n"] += 1
        return Channel3Result([{"id": "ch3_1", "name": "Jazz Vinyl Box"}], CHANNEL3_OK, "")

    with patch.object(Channel3SearchClient, "search_products_with_status", _fake):
        await channel3_module.search_products_with_status("  Jazz   Vinyl ", 5)
        again = await channel3_module.search_products_with_status("jazz vinyl", 5)

    assert again.items and again.status == CHANNEL3_OK
    assert calls["n"] == 1, "归一化后的同一 query 应命中缓存"
    channel3_module._CACHE.clear()


@pytest.mark.asyncio
async def test_circuit_breaker_skips_network_after_error():
    """熔断：一次失败后 60s 内不再发真实请求，直接降级（status 仍如实报 error）。

    这防止「每次点重新生成都白等 3–20s」——失败过一次就把兜底从"慢"变成"立刻"，
    但绝不把 error 伪装成 ok（status/message 都是诚实的）。
    """
    client = _client()
    client._sdks = [None] * len(client._contexts)
    calls = {"n": 0}

    def _boom(self, query, limit, index):  # noqa: ARG001
        calls["n"] += 1
        raise ConnectionError("network unreachable (simulated)")

    with patch.object(Channel3SearchClient, "_search_sync", _boom):
        first = await client.search_products_with_status("ping", 3)
        second = await client.search_products_with_status("ping", 3)

    assert first.status == CHANNEL3_ERROR
    assert second.status == CHANNEL3_ERROR
    assert "熔断" in second.message, second.message
    assert calls["n"] == 1, "第二次调用应被熔断短路，不再触发真实网络请求"


def test_dns_pin_pins_api_host_and_ignores_non_pin_hosts():
    """CHANNEL3_DNS_PIN 生效：api.trychannel3.com 解析被固定到真实 IP，非 pin 域名不受影响。

    2026-11-10 实测：本机网关把 api.trychannel3.com 的 CNAME 目标
    （d-aklj24vug8.execute-api.us-east-1.amazonaws.com）解析成 127.0.0.1 且时好时坏，
    而 8.8.8.8 / --resolve 均能拿真实 AWS IP。应用层 pin 只固定 connect 阶段的目标 IP，
    TLS 的 SNI/Host/证书校验仍基于域名本身 —— 不关校验、不改 hosts、无需管理员权限。
    """
    import socket as socket_module

    with patch.dict(os.environ, {"CHANNEL3_DNS_PIN": "3.212.207.12,3.230.231.112"}):
        config = Channel3Config.from_env()
        assert config.dns_pin == ("3.212.207.12", "3.230.231.112")
        Channel3SearchClient(config)  # __init__ 会安装全局 pin
        try:
            api_addrinfo = socket_module.getaddrinfo("api.trychannel3.com", 443)
            cname_addrinfo = socket_module.getaddrinfo(
                "d-aklj24vug8.execute-api.us-east-1.amazonaws.com", 443
            )
        finally:
            channel3_module._install_dns_pin([])  # 恢复系统解析，避免污染其他测试

    resolved = {info[4][0] for info in api_addrinfo}
    assert "3.212.207.12" in resolved, resolved
    assert "127.0.0.1" not in resolved, "pin 生效后不应再出现被污染的 127.0.0.1"
    cname_resolved = {info[4][0] for info in cname_addrinfo}
    assert cname_resolved == {"3.212.207.12"}, cname_resolved
    # 非 pin 域名必须走原 getaddrinfo（localhost 在 hosts 里，不依赖外网，稳定可断网）
    passthrough = socket_module.getaddrinfo("localhost", 80)
    assert passthrough, "非 pin 域名必须走原 getaddrinfo"

@pytest.mark.asyncio
async def test_planner_surfaces_channel3_error_status():
    """planner 降级时把 channel3_status 进字段，note 说清"暂时不可用"。"""
    with patch(
        "app.services.channel3.search_products_with_status",
        new=AsyncMock(return_value=Channel3Result([], CHANNEL3_ERROR, "boom")),
    ):
        result = await generate_dual_plans(
            intent_query="环保桌游奖品", constraints=[], use_channel3=True
        )

    retrieval = result["retrieval"]
    assert retrieval["channel3_status"] == CHANNEL3_ERROR, retrieval
    assert "error" in retrieval["note"]
    assert "本地" in retrieval["note"], "降级必须说清用了什么后备"
    assert retrieval["sponsor_related"] == []
@pytest.mark.asyncio
async def test_cached_query_bypasses_open_circuit_but_fresh_query_short_circuits():
    """时序根因回归（2026-11-14 排查）：缓存命中优先级高于熔断，
    熔断期内已缓存 query 秒回 ok、未缓存新 query 被短路 error。

    这正是「团购 Channel3 error、单人可用」现象的机制差 —— 团购与单人走同一条
    _retrieve_catalog 路径（无路径差异），差异来自：
      - 单人演示词（台灯/伞/包）在 30min 缓存里 → 命中缓存绕过熔断；
      - 团购新词（马克杯/充电宝）不在缓存 → 撞上 60s 熔断窗口 → 短路 error。
    真正的触发源是「某次真实请求瞬时失败」→ 熔断打开；与团购/单人语义无关。
    """
    channel3_module._CACHE.clear()
    # 预置一条「已成功的历史词」（30min TTL，ok 才入缓存）
    channel3_module._CACHE[channel3_module._cache_key("desk lamp", 20)] = {
        "items": [{"id": "ch3_l1", "name": "LED Desk Lamp"}],
        "status": CHANNEL3_OK,
        "message": "",
        "timestamp": time.time(),
    }
    # 模拟熔断打开：60s 内刚失败过一次（模块级全局，测试后必须复位）
    channel3_module._last_failure_at = time.monotonic()
    try:
        cached = await channel3_module.search_products_with_status("desk lamp", 20)
        fresh = await channel3_module.search_products_with_status("power bank", 20)
    finally:
        channel3_module._last_failure_at = None
        channel3_module._CACHE.clear()

    assert cached.status == CHANNEL3_OK and cached.items, (
        "已缓存历史词应绕过熔断秒回（缓存是之前真取到的事实，不需要新网络请求）"
    )
    assert fresh.status == CHANNEL3_ERROR and "熔断" in fresh.message, (
        "未缓存新词应被熔断短路 → 这正是团购新词 error、单人演示词 ok 的根因"
    )