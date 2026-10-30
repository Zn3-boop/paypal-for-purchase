

"""授权边界主线测试。

覆盖契约核心保证：
  1. 信任分 A/B/C/D 级证据闸门（C 级微调不跨门槛，D 级完全不改）
  2. 目标约束记忆（"存钱买相机" -> 冲突拦截）
  3. 冲动消费拦截（行为信号版：深夜+连续加购+超预算+储蓄冲突）
  4. 支付子图节点（mock 模式全链路）
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ["LLM_MODE"] = "mock"
os.environ["PAYMENT_MODE"] = "mock"

from app.services.paypal.authz import TrustProfile
from app.services.payment_agent.evidence import (
    EvidenceGrade,
    EvidenceMessage,
    apply_evidence,
    build_order_outcome_evidence,
    build_rule_inference_evidence,
    build_user_decision_evidence,
)
from app.services.payment_agent.goals import GoalMemory, SavingGoal
from app.services.payment_agent.impulse import (
    PurchaseSignal,
    check_impulse,
)


def test_generic_llm_category_uses_query_keywords():
    """泛化 LLM 类别不能覆盖 query 中更具体的商品类别。"""
    from app.services.payment_agent.nodes import _normalize_category

    assert _normalize_category("purchase", "帮我买一张爵士唱片") == "music"
    assert _normalize_category("payment", "订一家意餐餐厅") == "dining"
    assert _normalize_category("purchase", "买一个鼠标") == "merch"


def _dummy_intent():
    from app.services.paypal.config import PaymentIntent

    return PaymentIntent(
        id="i1", user_id="u1", description="测试支付", amount="10.00"
    )


# ============================================================
# 1. 信任分证据闸门
# ============================================================

class TestEvidenceGate:
    def test_d_grade_never_changes_trust(self):
        """D 级低置信推论完全不改信任分（腐烂记忆防污染）。"""
        t = TrustProfile(trust_score=0.3)
        ev = EvidenceMessage(
            grade=EvidenceGrade.D,
            intent_id="i1",
            user_id="u1",
            claim="LLM 猜测用户可能想买",
            delta=0.5,
        )
        new_t, c_delta = apply_evidence(t, evidence=ev)
        assert new_t.trust_score == 0.3
        assert c_delta == 0.0

    def test_c_grade_cannot_cross_auto_threshold(self):
        """C 级规则推断绝不把低分用户推过自动放行门槛。"""
        t = TrustProfile(trust_score=0.3)
        ev = build_rule_inference_evidence(
            _dummy_intent(), claim="规则推断用户偏好", delta=0.4
        )
        new_t, _ = apply_evidence(t, evidence=ev)
        assert new_t.trust_score <= 0.32  # 单次 delta cap 0.02

    def test_b_grade_success_updates_trust(self):
        """B 级订单事实：成功捕获提升信任分。"""
        t = TrustProfile(trust_score=0.3)
        ev = build_order_outcome_evidence(_dummy_intent(), succeeded=True)
        new_t, _ = apply_evidence(t, evidence=ev)
        assert new_t.trust_score > 0.3
        assert new_t.accurate_actions == 1

    def test_a_grade_user_approval_strong_signal(self):
        """A 级用户显式批准：强正信号。"""
        t = TrustProfile(trust_score=0.3)
        ev = build_user_decision_evidence(_dummy_intent(), decision="approve")
        new_t, _ = apply_evidence(t, evidence=ev)
        assert new_t.trust_score > 0.3

    def test_a_grade_user_reject_strong_penalty(self):
        """A 级用户否决：强负信号 + failed_actions 计数。"""
        t = TrustProfile(trust_score=0.5)
        ev = build_user_decision_evidence(_dummy_intent(), decision="reject")
        new_t, _ = apply_evidence(t, evidence=ev)
        assert new_t.trust_score < 0.5
        assert new_t.failed_actions == 1


# ============================================================
# 2. 目标约束记忆
# ============================================================

class TestGoalMemory:
    def test_goal_from_text(self):
        mem = GoalMemory()
        goal = mem.set_goal_from_text("u1", "我要存钱买相机，目标 5000 元")
        assert goal is not None
        assert goal.target_amount == 5000.0

    def test_purchase_conflicts_with_goal(self):
        mem = GoalMemory()
        mem.set_goal(SavingGoal(user_id="u1", target_label="存钱买相机", target_amount=5000.0))
        blocked = mem.check_purchase("u1", category="music", amount=1000.0, monthly_spent=50.0)
        assert blocked, "大额消费应命中目标约束"

    def test_small_purchase_no_conflict(self):
        mem = GoalMemory()
        mem.set_goal(SavingGoal(user_id="u1", target_label="存钱买相机", target_amount=5000.0))
        blocked = mem.check_purchase("u1", category="donation", amount=5.0, monthly_spent=10.0)
        assert not blocked, "小额捐款不应触发约束"


# ============================================================
# 3. 冲动消费拦截（行为信号版）
# ============================================================

class TestImpulse:
    def test_triggers_on_three_signals(self):
        """深夜 + 连续加购 + 储蓄冲突 -> 拦截。"""
        history = [
            PurchaseSignal(item_id="a", price=10.0),
            PurchaseSignal(item_id="b", price=10.0),
            PurchaseSignal(item_id="c", price=10.0),
        ]
        result = check_impulse(
            item_name="黑胶唱片",
            amount=30.0,
            monthly_spent=120.0,
            monthly_budget=150.0,
            savings_conflict=["【目标:存钱买相机】大额消费"],
            history=history,
            now=_late_night(),
        )
        assert result.triggered
        assert len(result.signals) >= 3
        assert "深夜" in result.reason

    def test_no_trigger_with_few_signals(self):
        """仅超预算一项 -> 不拦截。"""
        result = check_impulse(
            item_name="唱片",
            amount=30.0,
            monthly_spent=140.0,
            monthly_budget=150.0,
            savings_conflict=[],
            history=[],
            now=_daytime(),
        )
        assert not result.triggered


def _late_night():
    from datetime import datetime

    return datetime(2026, 10, 25, 23, 30)


def _daytime():
    from datetime import datetime

    return datetime(2026, 10, 25, 14, 0)


# ============================================================
# 4. 支付子图节点全链路（mock 模式）
# ============================================================

class TestPaymentSubgraph:
    @pytest.mark.asyncio
    async def test_full_flow_mock(self):
        """parse_intent -> taste_select -> authority -> create -> capture -> explain。"""
        from app.services.payment_agent.nodes import (
            authority_check_node,
            explain_node,
            parse_intent_node,
            paypal_capture_node,
            paypal_create_node,
            taste_select_node,
        )

        state: dict = {
            "raw_query": "帮我买一张周五晚上听的爵士唱片",
            "user_id": "u-demo",
            "session_id": "s-demo",
            "monthly_spent": 20.0,
        }

        r = await parse_intent_node(state)
        assert r.get("shopping_intent", {}).get("category") == "music"
        assert r.get("shopping_intent", {}).get("llm_source") in ("real", "mock_rule")

        state = {**state, **r}
        from unittest.mock import AsyncMock, patch

        with patch(
            "app.services.channel3.search_products_for_query",
            new=AsyncMock(return_value=[]),
        ):
            r = await taste_select_node(state)
        assert r.get("shopping_selected", {}).get("name")
        assert r.get("shopping_rank_source") in ("local_ranker", "channel3")

        state = {**state, **r}
        r = await authority_check_node(state)
        authority = r.get("authority", {})
        assert authority.get("verdict") in ("allow_auto", "need_approval", "block")
        assert authority.get("reason")
        assert authority.get("llm_reason")

        state = {**state, **r, "shopping_intent_id": "intent-demo-1"}
        approval = _approve(state)
        state = {**state, **approval}
        r = await paypal_create_node(state)
        assert r.get("paypal_order_id")
        assert r.get("approval_url")

        state = {**state, **r}
        r = await paypal_capture_node(state)
        assert r.get("paypal_status") == "captured"
        assert r.get("decision_trace", {}).get("status") == "captured"

        state = {**state, **r}
        r = await explain_node(state)
        assert r.get("explanation", {}).get("text")
        assert r.get("trust_updated", {}).get("evidence_grade") == "B"


def _approve(state: dict) -> dict:
    """模拟 HITL 审批通过（复用 approval_gate_node）。"""
    from app.services.payment_agent.nodes import approval_gate_node

    return approval_gate_node({**state, "approval_decision": "approve"})


class TestExplainabilityPanel:
    """可解释性面板数据源：散落证据 -> 前端结构。"""

    def test_assemble_empty_state_returns_empty(self):
        from app.services.payment_agent.panel import assemble_explainability_panel

        panel = assemble_explainability_panel({})
        assert panel["item"]["name"] == ""
        assert panel["authority"]["verdict"] == ""
        assert panel["payment"]["order_id"] == ""
        assert panel["trust"]["new_score"] is None

    def test_assemble_full_state(self):
        from app.services.payment_agent.panel import assemble_explainability_panel

        state = {
            "shopping_selected": {
                "name": "Sinatra 经典爵士音乐会门票",
                "price": 45.0,
                "currency": "USD",
                "match_tags": ["爵士", "周五放松"],
            },
            "shopping_rank_source": "local_ranker",
            "shopping_anchors": ["爵士", "黑胶"],
            "authority": {
                "verdict": "need_approval",
                "rule": "every_time",
                "reason": "金额 45.00 USD 超过自动授权上限 20.00，需要你的确认",
                "llm_reason": "这笔钱接近你单笔预算上限，建议你确认一下。",
                "trust_score": 0.3,
                "goal_conflicts": [],
                "impulse": {"signals": []},
            },
            "approval_status": "approved",
            "approval_comment": "放心买",
            "paypal_order_id": "ORDER-MOCK-123",
            "capture_id": "CAP-MOCK-1",
            "paypal_status": "captured",
            "paypal_msg": "（mock）订单已捕获",
            "decision_trace": {"order_id": "ORDER-MOCK-123", "capture_id": "CAP-MOCK-1", "status": "captured"},
            "explanation": {"text": "这笔支付（Sinatra 经典爵士音乐会门票）的裁决是 need_approval。", "llm_source": "mock_rule"},
            "trust_updated": {"old_score": 0.3, "new_score": 0.38, "evidence_grade": "B", "evidence_claim": "订单结果成功"},
        }
        panel = assemble_explainability_panel(state)
        assert panel["item"]["name"] == "Sinatra 经典爵士音乐会门票"
        assert panel["authority"]["verdict"] == "need_approval"
        assert panel["authority"]["llm_reason"]
        assert panel["match_tags"] == ["爵士", "周五放松"]
        assert panel["payment"]["order_id"] == "ORDER-MOCK-123"
        assert panel["trust"]["new_score"] == 0.38

    def test_evidence_groups_order(self):
        from app.services.payment_agent.panel import evidence_groups_for_panel

        state = {
            "shopping_selected": {"name": "黑胶唱片", "price": 30.0, "match_tags": ["爵士"]},
            "shopping_rank_source": "local_ranker",
            "authority": {"verdict": "allow_auto", "reason": "信任达标", "llm_reason": "放心"},
            "paypal_order_id": "ORDER-MOCK-123",
            "paypal_status": "captured",
            "trust_updated": {"old_score": 0.3, "new_score": 0.38, "evidence_grade": "B"},
        }
        groups = evidence_groups_for_panel(state)
        kinds = [g["kind"] for g in groups]
        assert "authority" in kinds
        assert "selection" in kinds
        assert "payment" in kinds
        assert "trust" in kinds
        for g in groups:
            assert g["title"]
            assert all("label" in r and "value" in r for r in g["rows"])

    @pytest.mark.asyncio
    async def test_authority_block_when_goal_conflict(self):
        """命中目标约束 -> need_approval/block（不自动放行）。"""
        from app.services.payment_agent.goals import get_goal_memory
        from app.services.payment_agent.nodes import (
            authority_check_node,
            parse_intent_node,
            taste_select_node,
        )

        mem = get_goal_memory("s-goal")
        mem.set_goal(SavingGoal(user_id="u-goal", target_label="存钱买相机", target_amount=5000.0))
        state: dict = {
            "raw_query": "买一个意大利餐厅套餐",
            "user_id": "u-goal",
            "session_id": "s-goal",
            "monthly_spent": 0.0,
            "goal_memory": mem,
        }
        r = await parse_intent_node(state)
        state = {**state, **r}
        from unittest.mock import AsyncMock, patch

        with patch(
            "app.services.channel3.search_products_for_query",
            new=AsyncMock(return_value=[]),
        ):
            r = await taste_select_node(state)
        state = {**state, **r}
        r = await authority_check_node(state)
        verdict = r.get("authority", {}).get("verdict")
        assert verdict in ("need_approval", "block"), f"目标约束应阻止自动放行，got {verdict}"
        assert r.get("goal_conflicts"), "应有目标冲突理由"