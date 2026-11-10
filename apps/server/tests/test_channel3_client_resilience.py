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