"""PayPal service 层。

模块：
  - config: 配置 + PaymentIntent / ApprovalDecision 数据模型
  - client: PayPal REST API 客户端（Orders v2 + Payouts v1）
  - authz:  渐进式授权引擎（搬自 aits.copy22 好感度系统）
  - service: PaymentIntent 生命周期编排门面
"""
from app.services.paypal.client import PayPalClient, PayPalError
from app.services.paypal.config import (
    ApprovalDecision,
    PaymentIntent,
    PayPalConfig,
)
from app.services.paypal.service import PayPalPaymentService, get_payment_service

__all__ = [
    "PayPalClient",
    "PayPalError",
    "PayPalConfig",
    "PaymentIntent",
    "ApprovalDecision",
    "PayPalPaymentService",
    "get_payment_service",
]