"""PayPal service 配置与数据模型。

De-risk 已验证 (2026-10-25):
  - Orders v2 create -> approval URL ✅
  - capture 端点通道 ✅ (未审批时返回预期的 ORDER_NOT_APPROVED)
  - Payouts v1 -> HTTP 201 ✅ (注意字段是 receiver 不是 recipient)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.core.config import get_settings

PayPalMode = Literal["sandbox", "live"]


@dataclass(frozen=True)
class PayPalConfig:
    mode: PayPalMode
    client_id: str
    secret: str
    api_base: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> PayPalConfig:
        settings = get_settings()
        mode: PayPalMode = settings.paypal_mode if settings.paypal_mode in ("sandbox", "live") else "sandbox"
        if mode == "sandbox":
            api_base = settings.paypal_sandbox_api.rstrip("/")
            client_id = settings.paypal_sandbox_client_id
            secret = settings.paypal_sandbox_secret
        else:
            api_base = (settings.paypal_rest_api or "https://api-m.paypal.com").rstrip("/")
            client_id = settings.paypal_sandbox_client_id  # 生产凭据请通过环境变量覆盖
            secret = settings.paypal_sandbox_secret
        return cls(mode=mode, client_id=client_id, secret=secret, api_base=api_base)

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.secret)


# ============================================================
# 数据模型 —— agentic 支付的核心状态
# ============================================================

PaymentStatus = Literal[
    "proposed",    # agent 推荐了一笔支付，等待授权判定
    "pending_approval",   # 需要人类审批（HITL）
    "approved",    # 已审批，可执行 capture
    "created",     # Orders v2 订单已创建（paypal_order_id 已生成）
    "captured",    # 已捕获（钱已实际移动）
    "failed",
    "cancelled",
    "refunded",
]


@dataclass
class PaymentIntent:
    """一次 agent 发起的支付意图（用户视角的语义），与 PayPal order 一一对应。"""

    id: str
    user_id: str
    # 业务语义
    description: str
    amount: str            # "25.00"
    currency: str = "USD"
    recipient_label: str = ""   # "小李" / "Cleaner"（人类可读）
    # agentic 证据（可解释性面板数据源）
    rationale: str = ""        # agent 为什么推荐这笔支付
    evidence: dict = field(default_factory=dict)  # 品味锚点/依据/匹配分
    source_agent: str = "taste_agent"
    # 授权与状态
    status: PaymentStatus = "proposed"
    auth_rule: str = ""        # 命中哪条授权规则（every_time / under_threshold / blocked）
    risk_level: str = "low"    # low / medium / high
    paypal_order_id: str = ""
    approval_url: str = ""
    capture_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    # 幂等与审计
    idempotency_key: str = ""


@dataclass
class ApprovalDecision:
    """渐进式授权判定结果（好感度系统 → 支付授权边界）。"""

    allow_auto: bool          # True = 无需人类审批
    rule: str                 # "every_time" / "under_threshold" / "blocked"
    reason: str               # 人类可读的解释
    max_auto_amount: str = "0.00"
    trust_score: float = 0.0  # 0~1，由历史准确率演化（搬自好感度系统）