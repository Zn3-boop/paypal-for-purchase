"""PayPal service 层单元测试。

覆盖：
  - 渐进式授权引擎：every_time / under_threshold / blocked 三条路径
  - 信任分数演化：成功后加分、失败重罚、阈值只减不增
  - PaymentIntent 生命周期：propose -> authorize -> create -> capture（client 用 fake）
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from app.services.paypal.authz import ProgressiveAuthzEngine, TrustProfile
from app.services.paypal.client import PayPalError
from app.services.paypal.service import PayPalPaymentService

os.environ.setdefault("PAYPAL_TRUST_INIT", "0.3")
os.environ.setdefault("PAYPAL_TRUST_BONUS", "0.08")
os.environ.setdefault("PAYPAL_TRUST_PENALTY", "0.25")
os.environ.setdefault("PAYPAL_AUTO_THRESHOLD", "20.0")
os.environ.setdefault("PAYPAL_AUTO_MIN_TRUST", "0.6")


# ============================================================
# 渐进式授权引擎
# ============================================================

class TestProgressiveAuthz:
    def test_new_user_defaults_to_manual_approval(self):
        """信任分数不足 → 每笔都需人工审批（默认安全）。"""
        decision = ProgressiveAuthzEngine.decide(
            amount=5.0, trust=TrustProfile(trust_score=0.3)
        )
        assert decision.allow_auto is False
        assert decision.rule == "every_time"

    def test_trusted_user_small_amount_auto_approved(self):
        """信任达标 + 金额在阈值内 → 自动放行。"""
        decision = ProgressiveAuthzEngine.decide(
            amount=15.0, trust=TrustProfile(trust_score=0.8, auto_threshold=20.0)
        )
        assert decision.allow_auto is True
        assert decision.rule == "under_threshold"

    def test_large_amount_requires_approval_even_for_trusted(self):
        """信任再高，超过阈值仍需审批。"""
        decision = ProgressiveAuthzEngine.decide(
            amount=50.0, trust=TrustProfile(trust_score=0.95, auto_threshold=20.0)
        )
        assert decision.allow_auto is False
        assert decision.rule == "every_time"

    def test_blocked_rule_wins(self):
        """命中目标约束 → 无条件拦截。"""
        decision = ProgressiveAuthzEngine.decide(
            amount=1.0,
            trust=TrustProfile(trust_score=0.95, auto_threshold=20.0),
            blocked_rules=["目标: 存钱买相机"],
        )
        assert decision.allow_auto is False
        assert decision.rule == "blocked"

    def test_trust_evolves_after_result(self):
        """成功后加分，失败重罚。"""
        t = TrustProfile(trust_score=0.5)
        ok = ProgressiveAuthzEngine.after_result(t, succeeded=True)
        assert ok.trust_score > 0.5
        fail = ProgressiveAuthzEngine.after_result(t, succeeded=False)
        assert fail.trust_score < 0.5

    def test_threshold_only_decreases(self):
        """阈值只减不增。"""
        t = TrustProfile(auto_threshold=20.0)
        lowered = ProgressiveAuthzEngine.lower_threshold(t, 5.0)
        assert lowered.auto_threshold == 5.0
        raised = ProgressiveAuthzEngine.lower_threshold(lowered, 100.0)
        assert raised.auto_threshold == 5.0
# ============================================================
# PaymentIntent 生命周期（fake client）
# ============================================================

class FakePayPalClient:
    """不触网的 fake client，模拟 create/capture。"""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.captured: list[dict] = []
        self.fail_capture = False

    async def create_order(self, **kwargs):
        order = {
            "id": "ORDER-123",
            "status": "CREATED",
            "links": [{"rel": "payer-action", "href": "https://sandbox.approve/ORDER-123"}],
        }
        self.created.append(order)
        return order

    async def capture_order(self, order_id: str):
        if self.fail_capture:
            raise PayPalError("ORDER_NOT_APPROVED", status_code=422)
        result = {
            "status": "COMPLETED",
            "purchase_units": [
                {"payments": {"captures": [{"id": "CAP-999", "status": "COMPLETED"}]}}
            ],
        }
        self.captured.append(result)
        return result

    async def close(self) -> None:
        return None

    @staticmethod
    def extract_approval_url(order: dict) -> str:
        for link in order.get("links", []):
            if link.get("rel") == "payer-action":
                return link["href"]
        return ""

    @staticmethod
    def extract_capture_id(order: dict) -> str:
        for pu in order.get("purchase_units", []):
            for pay in pu.get("payments", {}).get("captures", []):
                return pay["id"]
        return ""


@pytest.fixture
def service() -> PayPalPaymentService:
    return PayPalPaymentService(client=FakePayPalClient())


class TestPaymentLifecycle:
    @pytest.mark.asyncio
    async def test_full_flow(self, service):
        """propose -> authorize -> create -> capture 全链路。"""
        intent = service.propose_payment(
            user_id="u1",
            description="给社区食堂捐款",
            amount="10.00",
            recipient_label="社区食堂",
            rationale="用户每周固定捐款",
        )
        assert intent.status == "proposed"

        intent, reason = service.authorize_payment(intent.id)
        assert intent.status == "pending_approval"
        assert "需要你的确认" in reason

        intent, msg = await service.create_payment_order(intent.id)
        assert intent.status == "created"
        assert intent.paypal_order_id == "ORDER-123"
        assert intent.approval_url.startswith("https://sandbox.approve/")

        intent, msg = await service.capture_payment_order(intent.id)
        assert intent.status == "captured"
        assert intent.capture_id == "CAP-999"

    @pytest.mark.asyncio
    async def test_capture_unapproved_fails_gracefully(self, service):
        """未审批 → capture 失败但返回可读错误（不抛异常）。"""
        intent = service.propose_payment(user_id="u1", description="x", amount="5.00")
        intent, _ = service.authorize_payment(intent.id)
        intent, _ = await service.create_payment_order(intent.id)
        service._client.fail_capture = True  # type: ignore[attr-defined]
        intent, msg = await service.capture_payment_order(intent.id)
        assert intent.status == "failed"
        assert "尚未" in msg

    @pytest.mark.asyncio
    async def test_trust_rises_after_successful_capture(self, service):
        """成功 capture 后信任分数提升。"""
        intent = service.propose_payment(user_id="u2", description="x", amount="5.00")
        intent, _ = service.authorize_payment(intent.id)
        intent, _ = await service.create_payment_order(intent.id)
        intent, _ = await service.capture_payment_order(intent.id)
        trust = service.get_trust("u2")
        assert trust.trust_score > 0.3