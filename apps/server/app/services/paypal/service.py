"""PayPal agentic payment service —— PaymentIntent 生命周期编排。

流程：
  1. propose_payment(): 接收 agent 推荐 -> 生成 PaymentIntent (status=proposed)
  2. authorize_payment(): 渐进式授权判定 -> pending_approval 或 approved
  3. create_payment_order(): 创建 Orders v2 -> approval_url (status=created)
  4. capture_payment_order(): 买家审批后 capture (status=captured)

所有步骤返回 (intent, explanation) ；失败时返回 (intent, error_explanation)，
绝不抛出未处理异常 —— 支付链路采用显式降级，避免硬失败。

NOTE: 使用进程内轻量存储（演示用）。生产可替换为 SQLite/Postgres。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.paypal.authz import ProgressiveAuthzEngine, TrustProfile
from app.services.paypal.client import PayPalClient, PayPalError
from app.services.paypal.config import PaymentIntent

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PayPalPaymentService:
    def __init__(
        self,
        *,
        client: PayPalClient | None = None,
        intent_store: dict[str, PaymentIntent] | None = None,
        trust_store: dict[str, TrustProfile] | None = None,
    ) -> None:
        self._client = client or PayPalClient()
        # 进程内存储：intent_id -> PaymentIntent
        self._intents: dict[str, PaymentIntent] = intent_store if intent_store is not None else {}
        # user_id -> TrustProfile
        self._trust: dict[str, TrustProfile] = trust_store if trust_store is not None else {}

    async def close(self) -> None:
        await self._client.close()

    def propose_payment(
        self,
        *,
        user_id: str,
        description: str,
        amount: str,
        currency: str = "USD",
        recipient_label: str = "",
        rationale: str = "",
        evidence: dict | None = None,
        idempotency_key: str = "",
    ) -> PaymentIntent:
        """[1] agent 推荐一笔支付 -> PaymentIntent(proposed)。"""
        intent = PaymentIntent(
            id=uuid.uuid4().hex[:12],
            user_id=user_id,
            description=description,
            amount=amount,
            currency=currency,
            recipient_label=recipient_label,
            rationale=rationale,
            evidence=evidence or {},
            status="proposed",
            idempotency_key=idempotency_key or uuid.uuid4().hex,
            created_at=_now(),
            updated_at=_now(),
        )
        self._intents[intent.id] = intent
        logger.info("propose intent=%s user=%s amount=%s %s", intent.id, user_id, amount, currency)
        return intent

    def authorize_payment(
        self, intent_id: str, *, blocked_rules: list[str] | None = None
    ) -> tuple[PaymentIntent, str]:
        """[2] 渐进式授权判定 -> approved(自动) 或 pending_approval。"""
        intent = self._get(intent_id)
        trust = self._trust.get(intent.user_id, TrustProfile())
        decision = ProgressiveAuthzEngine.decide(
            amount=float(intent.amount),
            trust=trust,
            blocked_rules=blocked_rules,
            currency=intent.currency,
        )
        intent.status = "approved" if decision.allow_auto else "pending_approval"
        intent.auth_rule = decision.rule
        intent.risk_level = "low" if decision.allow_auto else "medium"
        intent.updated_at = _now()
        return intent, decision.reason

    async def create_payment_order(
        self, intent_id: str, *, return_url: str = "", cancel_url: str = ""
    ) -> tuple[PaymentIntent, str]:
        """[3] 创建 Orders v2 -> approval_url (status=created)。"""
        intent = self._get(intent_id)
        try:
            order = await self._client.create_order(
                amount=intent.amount,
                currency=intent.currency,
                description=intent.description,
                reference_id=intent.id,
                brand_name="Channel3 Agent",
                return_url=return_url or f"https://example.com/orders/{intent.id}/return",
                cancel_url=cancel_url or f"https://example.com/orders/{intent.id}/cancel",
            )
        except PayPalError as exc:
            intent.status = "failed"
            intent.updated_at = _now()
            return intent, f"PayPal 订单创建失败: {exc}"

        intent.paypal_order_id = order.get("id", "")
        intent.approval_url = self._client.extract_approval_url(order)
        intent.status = "created"
        intent.updated_at = _now()
        logger.info("created paypal order %s for intent=%s", intent.paypal_order_id, intent.id)
        return intent, "PayPal 订单已创建，等待买家审批"

    async def capture_payment_order(self, intent_id: str) -> tuple[PaymentIntent, str]:
        """[4] 审批后 capture (status=captured)。"""
        intent = self._get(intent_id)
        if not intent.paypal_order_id:
            intent.status = "failed"
            intent.updated_at = _now()
            return intent, "尚未创建 PayPal 订单，无法 capture"
        try:
            result = await self._client.capture_order(intent.paypal_order_id)
        except PayPalError as exc:
            intent.status = "failed"
            intent.updated_at = _now()
            if "ORDER_NOT_APPROVED" in str(exc):
                return intent, "买家尚未在 PayPal 审批该订单，请先完成审批"
            return intent, f"PayPal capture 失败: {exc}"

        intent.capture_id = self._client.extract_capture_id(result)
        intent.status = "captured"
        intent.updated_at = _now()
        self._record_result(intent.user_id, succeeded=True)
        return intent, f"支付成功（capture id={intent.capture_id}）"

    def _get(self, intent_id: str) -> PaymentIntent:
        intent = self._intents.get(intent_id)
        if intent is None:
            raise KeyError(f"PaymentIntent 不存在: {intent_id}")
        return intent

    def _record_result(self, user_id: str, *, succeeded: bool) -> None:
        trust = self._trust.get(user_id, TrustProfile())
        self._trust[user_id] = ProgressiveAuthzEngine.after_result(trust, succeeded=succeeded)

    def get_intent(self, intent_id: str) -> PaymentIntent | None:
        return self._intents.get(intent_id)

    def get_trust(self, user_id: str) -> TrustProfile:
        return self._trust.get(user_id, TrustProfile())

    def trust_snapshot(self) -> dict[str, Any]:
        return {
            uid: {
                "trust_score": t.trust_score,
                "accurate_actions": t.accurate_actions,
                "failed_actions": t.failed_actions,
                "auto_threshold": t.auto_threshold,
            }
            for uid, t in self._trust.items()
        }


# 模块级单例，避免重复创建服务。
_payment_service: PayPalPaymentService | None = None


def get_payment_service() -> PayPalPaymentService:
    global _payment_service
    if _payment_service is None:
        _payment_service = PayPalPaymentService()
    return _payment_service