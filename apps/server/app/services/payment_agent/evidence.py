"""信任分 A/B/C/D 级证据闸门 —— 授权边界主线的最值钱一条。

搬自 ecv 四级证据记忆（D 级低置信推论禁止写入长期记忆，防止"腐烂记忆"
污染），把同一套闸门套在信任分上：

  一个不会因为一次侥幸就放宽你权限的代理。

等级定义（EvidenceGrade）：
  - A.grade_user_decision  用户显式批准 / 否决 / 修改金额  → 强信号，可大幅升降信任分
  - B.grade_order_outcome  订单完成 / 退款 / 重复失败事实     → 事实信号，中等幅度
  - C.grade_rule_inference 规则推断（消息意图、行为信号）     → 弱信号，仅微调且受限
  - D.grade_llm_low_conf    LLM 低置信推论                   → 禁止写入长期记忆

闸门规则（GateRules）：
  1. 信任分【只有】A/B 级证据可以解锁自动放行——C 级可微调但绝不会推过门槛。
  2. C 级调整有硬上限（DELTA_CAP_C）。
  3. D 级完全不改变 TrustProfile（只记录日志）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from app.services.paypal.authz import (
    TrustProfile,
    TRUST_CORRECT_BONUS,
    TRUST_FAIL_PENALTY,
)
from app.services.paypal.config import PaymentIntent

logger = logging.getLogger(__name__)

# C 级证据的单次与累计上限（信任分 0~1）
DELTA_CAP_C_SINGLE = 0.02
DELTA_CAP_C_TOTAL = 0.05
# 自动放行门槛（与 authz 保持一致；如需覆盖请统一改 authz）
AUTO_MIN_TRUST = 0.6


class EvidenceGrade(str, Enum):
    A = "A"  # 用户显式决定
    B = "B"  # 订单事实
    C = "C"  # 规则推断
    D = "D"  # LLM 低置信推论


@dataclass(frozen=True)
class EvidenceMessage:
    """一条证据：谁在何时、以什么等级的置信，主张什么事实。"""

    grade: EvidenceGrade
    intent_id: str
    user_id: str
    claim: str            # 人类可读的"发生了什么"
    delta: float = 0.0    # 对 trust_score 的建议调整量（A/B 有效）
    meta: dict = None     # 附加信息（动作用户注释、订单状态等）

    def __post_init__(self):
        object.__setattr__(self, "grade", EvidenceGrade(self.grade))
        if self.meta is None:
            object.__setattr__(self, "meta", {})


def apply_evidence(
    trust: TrustProfile,
    *,
    evidence: EvidenceMessage,
    running_c_delta: float = 0.0,
) -> tuple[TrustProfile, float]:
    """把一条证据应用到 TrustProfile，返回 (新画像, 累计C级增量)。

    - A/B 级：真实改动 trust_score / accurate_actions / failed_actions。
    - C 级：仅在累计上限内微调，且不触碰 accurate/failed 计数
            （规则推断不算"行动结果"）。
    - D 级：完全不改画像（只记录日志，供会话内可见）。
    """
    grade = EvidenceGrade(evidence.grade)

    if grade is EvidenceGrade.D:
        logger.info(
            "D级证据丢弃（不入长期信任）：intent=%s claim=%r",
            evidence.intent_id, evidence.claim,
        )
        return trust, running_c_delta

    if grade is EvidenceGrade.C:
        return _apply_c_grade(trust, evidence, running_c_delta)

    if grade is EvidenceGrade.B:
        succeeded = evidence.delta >= 0
        return _after_result(trust, succeeded=succeeded), running_c_delta

    # A 级：用户显式决定是最强信号
    return _apply_user_decision(trust, evidence), running_c_delta


def _apply_c_grade(
    trust: TrustProfile, evidence: EvidenceMessage, running_c_delta: float
) -> tuple[TrustProfile, float]:
    """C 级规则推断：仅微调，绝不把用户推过自动放行门槛。"""
    if abs(evidence.delta) > DELTA_CAP_C_SINGLE:
        logger.warning(
            "C级证据超单次幅度上限 %.2f，忽略 delta=%.2f",
            DELTA_CAP_C_SINGLE, evidence.delta,
        )
        return trust, running_c_delta

    # 若信任分已达标则 C 级不再上调（防止反复微调堆积放宽权限）
    if trust.trust_score >= AUTO_MIN_TRUST and evidence.delta > 0:
        logger.info("C级证据忽略正向调整：信任分已达自动放行门槛")
        return trust, running_c_delta

    capped_total = running_c_delta + evidence.delta
    if capped_total > DELTA_CAP_C_TOTAL:
        logger.info("C级证据忽略：累计上限已达 %.2f", DELTA_CAP_C_TOTAL)
        return trust, running_c_delta

    proposed = max(0.0, min(1.0, trust.trust_score + evidence.delta))
    logger.info(
        "C级证据微调信任分 %.3f -> %.3f（累计 %.3f）",
        trust.trust_score, proposed, capped_total,
    )
    return (
        TrustProfile(
            trust_score=proposed,
            accurate_actions=trust.accurate_actions,
            failed_actions=trust.failed_actions,
            auto_threshold=trust.auto_threshold,
        ),
        capped_total,
    )


def _apply_user_decision(trust: TrustProfile, evidence: EvidenceMessage) -> TrustProfile:
    """A 级：用户显式批准 / 否决 / 修改金额。

    - 用户批准（delta>=0）：强正信号，高于规则/弱推论。
    - 用户否决（delta<0）：强负信号，大幅重罚。
    """
    if evidence.delta >= 0:
        return TrustProfile(
            trust_score=min(1.0, trust.trust_score + evidence.delta),
            accurate_actions=trust.accurate_actions + 1,
            failed_actions=trust.failed_actions,
            auto_threshold=trust.auto_threshold,
        )
    return TrustProfile(
        trust_score=max(0.0, trust.trust_score - abs(evidence.delta)),
        accurate_actions=trust.accurate_actions,
        failed_actions=trust.failed_actions + 1,
        auto_threshold=trust.auto_threshold,
    )


def _after_result(trust: TrustProfile, *, succeeded: bool) -> TrustProfile:
    """B 级订单事实：委托 authz.ProgressiveAuthzEngine（单一事实源）。"""
    from app.services.paypal.authz import ProgressiveAuthzEngine

    return ProgressiveAuthzEngine.after_result(trust, succeeded=succeeded)


def build_user_decision_evidence(
    intent: PaymentIntent, *, decision: str, comment: str = ""
) -> EvidenceMessage:
    """A 级：用户批准/否决一笔支付后的证据。"""
    approved = decision == "approve"
    return EvidenceMessage(
        grade=EvidenceGrade.A,
        intent_id=intent.id,
        user_id=intent.user_id,
        claim=f"用户{'批准' if approved else '否决'}支付：{intent.description}",
        delta=0.12 if approved else -0.4,
        meta={"decision": decision, "comment": comment},
    )


def build_order_outcome_evidence(
    intent: PaymentIntent, *, succeeded: bool, detail: str = ""
) -> EvidenceMessage:
    """B 级：订单完成/失败后的证据。"""
    return EvidenceMessage(
        grade=EvidenceGrade.B,
        intent_id=intent.id,
        user_id=intent.user_id,
        claim=f"订单{'成功' if succeeded else '失败'}：{detail or intent.description}",
        delta=TRUST_CORRECT_BONUS if succeeded else -TRUST_FAIL_PENALTY,
        meta={"succeeded": succeeded, "detail": detail},
    )


def build_rule_inference_evidence(
    intent: PaymentIntent, *, claim: str, delta: float
) -> EvidenceMessage:
    """C 级：规则推断（如冲动拦截触发的扣分）。"""
    return EvidenceMessage(
        grade=EvidenceGrade.C,
        intent_id=intent.id,
        user_id=intent.user_id,
        claim=claim,
        delta=delta,
        meta={"source": "rule_inference"},
    )