"""渐进式授权引擎 —— agent 花钱权限的答案。

搬自 aits.copy22 的好感度系统，翻译成支付授权边界：

  好感度（affection）            → 信任分数 trust_score
  长期相处后更了解用户            → 记录历史准确率后放宽自动授权
  "喜欢"体现为自动放行           → $threshold 以下且历史准确 → 免审批

规则（ApprovalRule）：
  - every_time:      无条件需要人类审批（新用户默认）
  - under_threshold: 金额 ≤ threshold 且信任分数达标 → 自动放行
  - blocked:         命中用户设定的拦截规则（如"存钱买相机"目标约束）

设计约束：
  1. 默认行为永远是 every_time（安全优先，绝不默认放开）
  2. 阈值只减不增（每次新增真实交易记录后，信任可能提升，但上限由用户设定）
  3. 每次人类审批 = 一次"正确预测"样本；每次失败/退款 = 一次惩罚
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.paypal.config import ApprovalDecision

logger = logging.getLogger(__name__)

# 信任分数演化参数（可环境变量覆盖，测试友好）
TRUST_INIT = float(__import__("os").getenv("PAYPAL_TRUST_INIT", "0.3"))
TRUST_CORRECT_BONUS = float(__import__("os").getenv("PAYPAL_TRUST_BONUS", "0.08"))
TRUST_FAIL_PENALTY = float(__import__("os").getenv("PAYPAL_TRUST_PENALTY", "0.25"))
AUTO_THRESHOLD_DEFAULT = float(__import__("os").getenv("PAYPAL_AUTO_THRESHOLD", "20.0"))
AUTO_MIN_TRUST = float(__import__("os").getenv("PAYPAL_AUTO_MIN_TRUST", "0.6"))


@dataclass(frozen=True)
class TrustProfile:
    """用户的支付信任画像（会话级演示用；生产可持久化到 DB）。"""

    trust_score: float = TRUST_INIT
    accurate_actions: int = 0
    failed_actions: int = 0
    auto_threshold: float = AUTO_THRESHOLD_DEFAULT  # 上限，只减不增


class ProgressiveAuthzEngine:
    """渐进式授权判定。线程安全（不维护共享可变状态）。"""

    @staticmethod
    def decide(
        *,
        amount: float,
        trust: TrustProfile,
        blocked_rules: list[str] | None = None,
        currency: str = "USD",
    ) -> ApprovalDecision:
        """输入金额与信任画像，输出授权判定。"""
        blocked_rules = blocked_rules or []

        # 1) 用户明确设定的拦截规则（目标约束记忆 / 冲动拦截）
        if blocked_rules:
            return ApprovalDecision(
                allow_auto=False,
                rule="blocked",
                reason="命中用户目标约束：" + "；".join(blocked_rules),
                max_auto_amount="0.00",
                trust_score=trust.trust_score,
            )

        # 2) 默认：every_time（新用户或信任不足 → 每笔人工审批）
        if trust.trust_score < AUTO_MIN_TRUST or amount > trust.auto_threshold:
            return ApprovalDecision(
                allow_auto=False,
                rule="every_time" if amount > trust.auto_threshold else "every_time",
                reason=(
                    f"金额 {amount:.2f} {currency} 超过自动授权上限 "
                    f"{trust.auto_threshold:.2f}，需要你的确认"
                    if amount > trust.auto_threshold
                    else f"信任分数 {trust.trust_score:.2f} 未达自动授权门槛，需要你的确认"
                ),
                max_auto_amount=f"{trust.auto_threshold:.2f}",
                trust_score=trust.trust_score,
            )

        # 3) under_threshold：信任达标且金额在阈值内 → 自动放行
        return ApprovalDecision(
            allow_auto=True,
            rule="under_threshold",
            reason=(
                f"信任分数 {trust.trust_score:.2f} 达标，"
                f"金额 {amount:.2f} {currency} 在自动授权上限 {trust.auto_threshold:.2f} 内"
            ),
            max_auto_amount=f"{trust.auto_threshold:.2f}",
            trust_score=trust.trust_score,
        )

    @staticmethod
    def after_result(trust: TrustProfile, *, succeeded: bool) -> TrustProfile:
        """交易结束后更新信任画像（每次准确动作加分，失败/退款重罚）。"""
        if succeeded:
            return TrustProfile(
                trust_score=min(1.0, trust.trust_score + TRUST_CORRECT_BONUS),
                accurate_actions=trust.accurate_actions + 1,
                failed_actions=trust.failed_actions,
                auto_threshold=trust.auto_threshold,
            )
        return TrustProfile(
            trust_score=max(0.0, trust.trust_score - TRUST_FAIL_PENALTY),
            accurate_actions=trust.accurate_actions,
            failed_actions=trust.failed_actions + 1,
            auto_threshold=trust.auto_threshold,
        )

    @staticmethod
    def lower_threshold(trust: TrustProfile, new_threshold: float) -> TrustProfile:
        """阈值只减不增：用户主动收紧上限。"""
        return TrustProfile(
            trust_score=trust.trust_score,
            accurate_actions=trust.accurate_actions,
            failed_actions=trust.failed_actions,
            auto_threshold=min(trust.auto_threshold, new_threshold),
        )