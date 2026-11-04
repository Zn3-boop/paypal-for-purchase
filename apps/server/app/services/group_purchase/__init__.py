"""团体采购协理 —— 多人协作采购的约束系统、方案生成、分摊与审计。

模块划分（对应执行清单）：
  A. constraints.py      约束系统（数据结构 + 状态管理 + 冲突检测）
  B. prompts.py          解析 Prompt 组（约束提取/冲突识别/变更解析）
  C. planner.py          双方案生成（权衡 + 风险）
  D. budget_guardrail.py 预算 Guardrail
  E. cart_editor.py      HITL 自然语言改单
  G. split_bill.py       多人分摊 + PayPal 分账
  H. order_lifecycle.py  订单生命周期
  I. audit_log.py        决策审计日志
  J. fallback.py         兜底逻辑
  
 新增模块（本周补全）：
   E1+E3. cart_modifier.py 对话式改单引擎（意图解析 + 编排执行）
   E2. session_state.py 会话状态持久化
"""
from app.services.group_purchase.audit_log import (
    AuditLogger,
    get_audit_logger,
    log_budget_violation,
    log_cart_modification,
    log_conflict_detection,
    log_constraint_change,
    log_fallback_trigger,
    log_payment_completion,
    log_plan_generation,
    log_review_confirmation,
)
from .budget_guardrail import (
    adjust_cart_to_budget,
    calc_total,
    check_budget,
)
from .constraints import (
    Constraint,
    ConstraintConflict,
    ConstraintManager,
    get_constraint_manager,
)
from .split_bill import (
    SplitResult,
    simulate_paypal_split_payment,
    split_bill,
    validate_split_consistency,
)
from .prompts import (
    B2_CONSTRAINT_EXTRACTION_PROMPT,
    B3_CONFLICT_DETECTION_PROMPT,
    B4_CHANGE_PARSING_PROMPT,
    C2_DUAL_PLAN_GENERATION_PROMPT,
    E1_CART_MODIFICATION_PROMPT,
    FALLBACK_TEMPLATES,
    GROUP_PURCHASE_SYSTEM_PROMPT,
)
from .planner import generate_dual_plans, prefilter_by_constraints
from .fallback import generate_fallback_message, suggest_constraint_relaxation

__all__ = [
    # A. 约束系统
    "Constraint",
    "ConstraintConflict",
    "ConstraintManager",
    "get_constraint_manager",
    # D. 预算 Guardrail
    "calc_total",
    "check_budget",
    "adjust_cart_to_budget",
    # G. 分摊
    "SplitResult",
    "split_bill",
    "simulate_paypal_split_payment",
    "validate_split_consistency",
    # I. 审计日志
    "AuditLogger",
    "get_audit_logger",
    "log_constraint_change",
    "log_plan_generation",
    "log_cart_modification",
    "log_review_confirmation",
    "log_payment_completion",
    "log_conflict_detection",
    "log_budget_violation",
    "log_fallback_trigger",
    # B/C/E/J. Prompts
    "GROUP_PURCHASE_SYSTEM_PROMPT",
    "B2_CONSTRAINT_EXTRACTION_PROMPT",
    "B3_CONFLICT_DETECTION_PROMPT",
    "B4_CHANGE_PARSING_PROMPT",
    "C2_DUAL_PLAN_GENERATION_PROMPT",
    "E1_CART_MODIFICATION_PROMPT",
    "FALLBACK_TEMPLATES",
    # C. Planner
    "generate_dual_plans",
    "prefilter_by_constraints",
    # J. Fallback
    "generate_fallback_message",
    "suggest_constraint_relaxation",
]

# 新增：会话状态管理
from .session_state import (
    GroupPurchaseSession,
    PlanItem,
    SplitPreview,
    get_or_create_session,
    get_session,
    update_session,
    delete_session,
    list_sessions,
)

# 新增：改单引擎
from .cart_modifier import (
    parse_modification_intent,
    execute_modifications,
    modify_cart_and_replan,
)

__all__ += [
    # E2. 会话状态
    "GroupPurchaseSession",
    "PlanItem",
    "SplitPreview",
    "get_or_create_session",
    "get_session",
    "update_session",
    "delete_session",
    "list_sessions",
    # E1+E3. 改单引擎
    "parse_modification_intent",
    "execute_modifications",
    "modify_cart_and_replan",
]