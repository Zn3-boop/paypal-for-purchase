"""授权边界主线 —— 支付子图服务层。

把 LPM 三个机制搬进支付场景：
  1. 好感度系统     -> 渐进式授权（paypal/authz.py）+ A/B 级证据闸门（evidence.py）
  2. 双层记忆       -> 目标约束记忆（goals.py）
  3. 情绪检测       -> 冲动消费拦截（impulse.py，行为信号版，无摄像头）

模块：
  - evidence: 信任分 A/B/C/D 级证据闸门（最值钱的一条）
  - goals:    结构化储蓄目标 + 冲突检测
  - impulse:  行为信号冲动拦截
  - nodes:    LangGraph 支付子图节点（parse_intent -> ... -> explain）
  - panel:    可解释性面板数据源（证据拼装给前端）
"""
from app.services.payment_agent.evidence import (
    EvidenceGrade,
    EvidenceMessage,
    apply_evidence,
    build_order_outcome_evidence,
    build_rule_inference_evidence,
    build_user_decision_evidence,
)
from app.services.payment_agent.goals import GoalMemory, SavingGoal, get_goal_memory
from app.services.payment_agent.impulse import (
    ImpulseCheckResult,
    PurchaseSignal,
    check_impulse,
)
from app.services.payment_agent.panel import (
    assemble_explainability_panel,
    evidence_groups_for_panel,
)

__all__ = [
    "EvidenceGrade",
    "EvidenceMessage",
    "apply_evidence",
    "build_order_outcome_evidence",
    "build_rule_inference_evidence",
    "build_user_decision_evidence",
    "GoalMemory",
    "SavingGoal",
    "get_goal_memory",
    "ImpulseCheckResult",
    "PurchaseSignal",
    "check_impulse",
    "assemble_explainability_panel",
    "evidence_groups_for_panel",
]