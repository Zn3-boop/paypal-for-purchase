"""支付子图 × 主图 集成测试（LangGraph 编译 + 路由 + 端到端 mock 流程）。

验证契约红线：
  - 支付意图被路由进支付子图（route_after_llm 分支）
  - payment_approval_gate 在 interrupt_before 列表中（HITL 复用）
  - 全链路 mock 可跑通（无外部搜索 key / 无模型 / 无真实 PayPal 凭据）
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ["LLM_MODE"] = "mock"
os.environ["PAYMENT_MODE"] = "mock"

from app.services.graph.graph import (
    _has_payment_intent,
    build_agent_graph,
    route_after_payment_approval,
    route_after_payment_authority,
)


class TestPaymentRouting:
    def test_payment_keyword_detected(self):
        """中文购买关键词 -> 支付意图。"""
        assert _has_payment_intent({"raw_query": "帮我买一张爵士唱片"})
        assert _has_payment_intent({"raw_query": "我要下单一个意餐套餐"})
        assert not _has_payment_intent({"raw_query": "推荐一部好看的电影"})

    def test_route_after_authority_block(self):
        """block -> memory_persist（终止，不创建订单）。"""
        assert route_after_payment_authority({"authority": {"verdict": "block"}}) == "memory_persist_node"

    def test_route_after_authority_allow_auto(self):
        """allow_auto -> 直接创建 PayPal 订单。"""
        assert route_after_payment_authority({"authority": {"verdict": "allow_auto"}}) == "payment_paypal_create"

    def test_route_after_authority_need_approval(self):
        """need_approval -> 进入审批闸门。"""
        assert route_after_payment_authority({"authority": {"verdict": "need_approval"}}) == "payment_approval_gate"

    def test_route_after_approval_approved(self):
        """审批通过 -> 创建 PayPal 订单。"""
        assert route_after_payment_approval({"approval_status": "approved"}) == "payment_paypal_create"

    def test_route_after_approval_rejected(self):
        """审批否决 -> 终止。"""
        assert route_after_payment_approval({"approval_status": "cancelled"}) == "memory_persist_node"


class TestPaymentSubgraphCompile:
    @pytest.fixture(autouse=True)
    def _force_local_catalog(self, monkeypatch):
        """E2E 全链路 mock：强制 Channel3 L1 空结果，选品稳定走本地 catalog。"""
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "app.services.channel3.search_products_for_query",
            AsyncMock(return_value=[]),
        )

    def test_graph_compiles_with_payment_nodes(self):
        """主图编译成功，且包含支付子图节点。"""
        graph = build_agent_graph()
        nodes = list(graph.get_graph().nodes.keys())
        assert "payment_parse_intent" in nodes
        assert "payment_taste_select" in nodes
        assert "payment_authority_check" in nodes
        assert "payment_approval_gate" in nodes
        assert "payment_paypal_create" in nodes
        assert "payment_paypal_capture" in nodes
        assert "payment_explain" in nodes

    def test_payment_gate_in_interrupt_before(self):
        """契约红线：payment_approval_gate 必须复用 interrupt_before 机制。"""
        from app.services.graph.graph import build_invoke_config

        graph = build_agent_graph()
        cfg = build_invoke_config("u-intr", "s-intr")
        state = {
            "raw_query": "帮我买一张周五晚上听的爵士唱片",
            "user_id": "u-intr",
            "session_id": "s-intr",
            "monthly_spent": 20.0,
        }
        # 第一次 invoke 应停在审批闸门（不产出订单）
        import asyncio

        first = asyncio.run(graph.ainvoke(state, cfg))
        # interrupt 状态：graph_state 中应保留支付判定字段
        snps = list(graph.get_state_history(cfg))
        assert snps, "中断后应存在 checkpoint"
        assert snps[0].next == ("payment_approval_gate",), "中断点必须是 payment_approval_gate"
        # 未提供审批决策时不应创建订单
        assert not first.get("paypal_order_id")


class TestPaymentEndToEnd:
    """端到端：HITL 中断 → 批准 → 捕获 → 解释 → 信任分回写。"""

    @pytest.fixture(autouse=True)
    def _force_local_catalog(self, monkeypatch):
        """E2E 全链路 mock：强制 Channel3 L1 空结果，选品稳定走本地 catalog。"""
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "app.services.channel3.search_products_for_query",
            AsyncMock(return_value=[]),
        )

    def _base_state(self, user: str):
        return {
            "raw_query": "帮我买一张周五晚上听的爵士唱片",
            "user_id": user,
            "session_id": user,
            "monthly_spent": 20.0,
        }

    def test_approve_flow_full_e2e(self):
        """批准路径：need_approval → 批准 → create → capture → explain。"""
        import asyncio
        from app.services.graph.graph import build_invoke_config
        from langgraph.types import Command

        async def _run():
            graph = build_agent_graph()
            cfg = build_invoke_config("u-e2e-ok", "s-e2e-ok")
            first = await graph.ainvoke(self._base_state("u-e2e-ok"), cfg)

            # 第一程：必须是 need_approval（新用户 trust=0.3 < 0.6）
            auth = first.get("authority") or {}
            assert auth.get("verdict") == "need_approval", f"新用户应需审批，got {auth.get('verdict')}"
            assert auth.get("reason"), "授权判定必须带 reason"
            assert not first.get("paypal_order_id"), "审批前不得创建订单"

            # 第二程：写入审批决策（Command(update=...) 是 langgraph 1.2.x 静态 interrupt 恢复标准）
            second = await graph.ainvoke(
                Command(update={"approval_decision": "approve", "approval_comment": "放心买"}),
                cfg,
            )
            assert second.get("approval_status") == "approved"
            assert second.get("paypal_order_id") == "ORDER-MOCK-123", (
                f"mock 创建订单，got {second.get('paypal_order_id')}"
            )
            assert second.get("capture_id") == "CAP-MOCK-1"
            dt = second.get("decision_trace") or {}
            assert dt.get("status") == "captured"
            assert dt.get("order_id") == "ORDER-MOCK-123"
            expl = second.get("explanation") or {}
            assert expl.get("text"), "事后解释必须存在"
            assert "爵士" in expl.get("text", ""), "解释应引用购买对象"
            assert second.get("trust_updated"), "信任分必须回写"
            return second

        second = asyncio.run(_run())
        # 信任分上升（A 级证据批准）
        tu = second["trust_updated"]
        assert tu.get("old_score", 1.0) < tu.get("new_score", 0.0), "批准后信任分应上升"

    def test_modify_flow_approved_with_evidence(self):
        """修改路径：need_approval → modify（改后放行）→ create → capture → explain。

        P2-9 的红线：modify 视作 A 级证据（approved + modified 标记），
        状态返回 approved 且继续走 PayPal 创建/捕获，而不是终止。
        """
        import asyncio
        from app.services.graph.graph import build_invoke_config
        from langgraph.types import Command

        async def _run():
            graph = build_agent_graph()
            cfg = build_invoke_config("u-e2e-mod", "s-e2e-mod")
            first = await graph.ainvoke(self._base_state("u-e2e-mod"), cfg)

            # 第一程：新用户需审批
            auth = first.get("authority") or {}
            assert auth.get("verdict") == "need_approval", f"新用户应需审批，got {auth.get('verdict')}"
            assert not first.get("paypal_order_id"), "审批前不得创建订单"

            # 第二程：modify（用户改金额后放行）
            second = await graph.ainvoke(
                Command(update={"approval_decision": "modify", "approval_comment": "帮我降到60以内"}),
                cfg,
            )
            # modify 应放行（approved），不是终止
            assert second.get("approval_status") == "approved", (
                f"modify 应审批通过，got {second.get('approval_status')}"
            )
            # 金额修正证据已回写：A 级证据应用摘要带 modified 标记
            applied = second.get("approval_evidence_applied") or {}
            assert applied.get("claim") == "用户修改金额后批准支付", (
                f"modify 证据 claim 应标注改后放行，got {applied.get('claim')}"
            )
            # 继续走 PayPal 链路
            assert second.get("paypal_order_id") == "ORDER-MOCK-123", (
                f"modify 后应创建订单，got {second.get('paypal_order_id')}"
            )
            assert second.get("capture_id") == "CAP-MOCK-1"
            dt = second.get("decision_trace") or {}
            assert dt.get("status") == "captured"
            # 事后解释存在（引用商品）
            expl = second.get("explanation") or {}
            assert expl.get("text"), "modify 之后仍应有事后解释"
            assert "爵士" in expl.get("text", ""), "解释应引用购买对象"
            # 信任分回写：modify 属 A 级证据批准，应上升
            tu = second.get("trust_updated") or {}
            assert tu.get("new_score", 0.0) > tu.get("old_score", 1.0), "modify（核准）后信任分应上升"
            return second

        second = asyncio.run(_run())
        assert second.get("approval_status") == "approved"

    def test_reject_flow_terminates(self):
        """否决路径：need_approval → 否决 → 终止（不创建订单）。"""
        import asyncio
        from app.services.graph.graph import build_invoke_config
        from langgraph.types import Command

        async def _run():
            graph = build_agent_graph()
            cfg = build_invoke_config("u-e2e-rej", "s-e2e-rej")
            await graph.ainvoke(self._base_state("u-e2e-rej"), cfg)
            second = await graph.ainvoke(
                Command(update={"approval_decision": "reject", "approval_comment": "超预算，不买"}),
                cfg,
            )
            assert second.get("approval_status") == "cancelled"
            assert not second.get("paypal_order_id"), "否决后不得创建订单"
            tu = second.get("trust_updated") or {}
            # 否决回写负证据（旧分 > 新分 或 未应用）
            if tu.get("new_score") is not None and tu.get("old_score") is not None:
                assert tu.get("old_score") >= tu.get("new_score"), "否决后信任分不应上升"

        asyncio.run(_run())

    def test_auto_flow_high_trust(self):
        """高信任小额自动放行：allow_auto → 直接 create（无中断）。"""
        import asyncio
        from app.services.graph.graph import build_invoke_config
        from app.services.paypal.authz import TrustProfile

        async def _run():
            # 用高信任 + 低额商品走 allow_auto：给 trust_profile 提升到 0.8
            graph = build_agent_graph()
            cfg = build_invoke_config("u-e2e-auto", "s-e2e-auto")
            from dataclasses import replace

            # 高信任 + 高自动上限（覆盖商品价 45)）：走 allow_auto
            tp = replace(TrustProfile(), trust_score=0.9, auto_threshold=100.0)
            state = self._base_state("u-e2e-auto")
            state["trust_profile"] = tp
            result = await graph.ainvoke(state, cfg)
            # allow_auto 无中断，一次跑完
            auth = result.get("authority") or {}
            assert auth.get("verdict") == "allow_auto", f"高信任应自动放行，got {auth.get('verdict')}"
            assert result.get("paypal_order_id") == "ORDER-MOCK-123"
            assert result.get("capture_id") == "CAP-MOCK-1"

        asyncio.run(_run())