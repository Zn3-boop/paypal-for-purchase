"""对话式改单引擎 —— E1+E3 模块：意图解析 + 编排执行。

对应执行清单：
  E1 改单意图解析（5 类指令：swap/delete/add/adjust_budget/adjust_constraint）
  E2 状态更新（复用 session_state）
  E3 编排执行（LLM 解析 → 执行修改 → 重算 → 审计埋点）

设计要点：
  - 复用 LLMProvider.chat() + llm_parse_with_retry() 进行结构化输出
  - JSON 解析失败时自动重试/修复
  - 执行成功后自动触发重算（budget/split）
  - 所有修改都记录审计日志
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.group_purchase.budget_guardrail import calc_total
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.fallback import generate_fallback_message
from app.services.group_purchase.prompts import (
    CartModificationOutput,
    CartModificationRequest,
    ModificationType,
)
from app.services.group_purchase.session_state import (
    GroupPurchaseSession,
    PlanItem,
    get_or_create_session,
    update_session,
)
from app.services.group_purchase.split_bill import split_bill
from app.services.group_purchase.audit_log import (
    log_cart_modification,
    log_plan_generation,
    log_budget_violation,
)
from app.services.shopper.catalog import get_catalog_items as get_local_catalog
from app.services.shopper.catalog_k1 import get_k1_items

logger = logging.getLogger(__name__)


async def parse_modification_intent(
    user_instruction: str,
    current_cart: list[PlanItem],
    catalog: list[dict[str, Any]] | None = None,
) -> CartModificationOutput:
    """E1：解析用户的改单指令。
    
    使用 LLM 进行结构化提取，自动重试/修复 JSON。
    """
    from app.services.llm.client import llm_parse_with_retry
    from app.services.group_purchase.prompts import E1_CART_MODIFICATION_PROMPT
    
    if catalog is None:
        catalog = get_local_catalog()
    
    # 构建当前购物车信息
    cart_blob = "\n".join([
        f"- [{it.id}] {it.name} (${it.price} x {it.quantity})"
        for it in current_cart
    ])
    
    prompt = f"{E1_CART_MODIFICATION_PROMPT}\n\n当前购物车:\n{cart_blob}\n\n用户指令：{user_instruction}"
    
    parsed, raw = await llm_parse_with_retry(
        prompt=prompt,
        resp_model=CartModificationOutput,
        max_retry=2,
        timeout=15.0,
    )
    
    if parsed is None:
        # 降级：尝试基于规则解析简单指令
        logger.warning("LLM 解析失败，尝试降级规则解析")
        parsed = _fallback_rule_based_parse(user_instruction, current_cart)
    
    return parsed


def _fallback_rule_based_parse(
    instruction: str,
    cart: list[PlanItem],
) -> CartModificationOutput:
    """规则降级解析（用于 LLM 失败时的兜底）。"""
    inst_lower = instruction.lower()
    modifications: list[CartModificationRequest] = []
    
    # 简单关键词匹配
    if "删" in inst_lower or "去掉" in inst_lower or "不要" in inst_lower:
        # 尝试找商品 ID
        for item in cart:
            if item.id in inst_lower or item.name.split()[0] in inst_lower:
                modifications.append(CartModificationRequest(
                    modification_type="delete_item",
                    target_item_id=item.id,
                    explanation=f"检测到删除指令：{instruction}",
                ))
                break
    
    if "加" in inst_lower or "补" in inst_lower or "再来" in inst_lower:
        modifications.append(CartModificationRequest(
            modification_type="add_item",
            explanation=f"检测到添加指令：{instruction}",
        ))
    
    if "换" in inst_lower or "换成" in inst_lower:
        modifications.append(CartModificationRequest(
            modification_type="swap_item",
            explanation=f"检测到替换指令：{instruction}",
        ))
    
    if "预算" in inst_lower and ("加" in inst_lower or "补" in inst_lower):
        modifications.append(CartModificationRequest(
            modification_type="adjust_budget",
            explanation=f"检测到预算调整：{instruction}",
        ))
    
    if not modifications:
        modifications.append(CartModificationRequest(
            modification_type="adjust_constraint",
            explanation=f"未知类型的修改：{instruction}",
        ))
    
    return CartModificationOutput(
        modifications=modifications,
        intent_summary=f"用户修改：{instruction[:50]}",
    )


async def execute_modifications(
    session_id: str,
    user_id: str,
    modifications: list[CartModificationRequest],
    catalog: list[dict[str, Any]] | None = None,
    constraints: list[Constraint] | None = None,
) -> dict[str, Any]:
    """E3：执行改单操作并重算方案。
    
    流程：
    1. 获取当前会话状态
    2. 依次执行每条修改（swap/delete/add/adjust_*）
    3. 重新计算总价和分摊
    4. 检查预算约束
    5. 更新会话状态
    6. 记录审计日志
    """
    session = get_or_create_session(session_id, user_id)
    session.save_snapshot(reason="modification_started")
    
    if catalog is None:
        catalog = get_local_catalog()
    
    # 获取当前购物车
    current_cart = session.get_latest_cart()
    old_total = calc_total([{"price": it.price, "quantity": it.quantity} for it in current_cart])
    
    warnings: list[str] = []
    
    # 依次执行修改
    for mod in modifications:
        if mod.modification_type == "delete_item":
            if mod.target_item_id:
                current_cart = [it for it in current_cart if it.id != mod.target_item_id]
        elif mod.modification_type == "swap_item":
            if mod.target_item_id and mod.replacement_item_id:
                # 在 catalog 中找到替换商品
                new_item_dict = next((c for c in catalog if c["id"] == mod.replacement_item_id), None)
                if new_item_dict:
                    # 替换旧商品
                    for i, item in enumerate(current_cart):
                        if item.id == mod.target_item_id:
                            current_cart[i] = PlanItem.from_dict(new_item_dict)
                            break
                else:
                    warnings.append(f"未找到替换商品 {mod.replacement_item_id}")
        elif mod.modification_type == "add_item":
            # 优先按 target_item_id 精确添加；LLM 未指定时降级为目录首个商品
            new_item_dict = None
            if mod.target_item_id:
                new_item_dict = next(
                    (c for c in catalog if c["id"] == mod.target_item_id), None
                )
                if new_item_dict is None:
                    warnings.append(f"未找到要添加的商品 {mod.target_item_id}")
            if new_item_dict is None and catalog:
                new_item_dict = catalog[0]
                current_cart.append(PlanItem.from_dict(new_item_dict))
                if not mod.target_item_id:
                    warnings.append("未指定添加的具体商品，已添加默认项")
            elif new_item_dict is not None:
                current_cart.append(PlanItem.from_dict(new_item_dict))
        elif mod.modification_type == "adjust_budget":
            if mod.new_constraint_value:
                # 查找预算约束并更新
                for c in session.constraints:
                    if c.type == "budget" and c.hard and c.status == "active":
                        c.numeric_value = float(mod.new_constraint_value)
                        c.status = "active"
                        break
        elif mod.modification_type == "adjust_constraint":
            # 通用约束调整
            pass
    
    new_total = calc_total([{"price": it.price, "quantity": it.quantity} for it in current_cart])
    delta = new_total - old_total
    
    # 预算检查
    budget_constraints = [c for c in session.constraints if c.type == "budget" and c.hard and c.status == "active"]
    budget_limit = min([c.numeric_value for c in budget_constraints if c.numeric_value]) if budget_constraints else None
    
    budget_ok = True
    fallback = None
    if budget_limit is not None and new_total > budget_limit:
        budget_ok = False
        warnings.append(f"超预算 {new_total - budget_limit:.2f}，请调整或增加预算")
        
        # J 模块：改单后 Guardrail 失败 → 触发兜底（二选一建议）
        fallback = generate_fallback_message(
            session.constraints,
            catalog,
            [],
            budget_limit,
        )
        fallback["status"] = "budget_violated_after_modification"
        fallback["message"] = (
            f"⚠️ 修改后总价 {new_total:.2f} 超出预算上限 {budget_limit:.2f}"
            f"（超出 {new_total - budget_limit:.2f}）。\n\n"
            f"{fallback['message']}"
        )
        
        # 记录预算违规审计
        log_budget_violation(
            session_id=session_id,
            user_id=user_id,
            actor="system",
            budget_limit=budget_limit,
            actual_total=new_total,
            resolution="用户需调整购物车或增加预算",
        )
    
    # 分摊计算
    split_preview = None
    if current_cart:
        member_ids = [c.source for c in session.constraints if c.source and c.status == "active"]
        if len(member_ids) >= 2:
            try:
                splits_result = split_bill(new_total, member_ids)
                member_splits = [
                    {"member_id": s["member_id"], "amount": s["amount"]}
                    for s in splits_result.splits
                ]
                split_preview = {
                    "member_splits": member_splits,
                    "total": new_total,
                }
                
                # 更新会话的分摊预览
                from app.services.group_purchase.session_state import SplitPreview
                session.split_preview = SplitPreview(member_splits=member_splits)
            except Exception as e:
                warnings.append(f"分摊计算失败：{e}")
    
    # 更新会话状态
    session.cart_items = current_cart
    session.state = "reviewing"
    session.save_snapshot(reason="modification_executed")
    update_session(session)
    
    # 审计日志
    log_cart_modification(
        session_id=session_id,
        user_id=user_id,
        actor="user",
        modification_type="batch_update",
        item_ids=[m.target_item_id or "(new)" for m in modifications],
        old_total=old_total,
        new_total=new_total,
    )
    
    return {
        "success": budget_ok or len(warnings) == 0,
        "message": f"修改完成，总价变化 {delta:+.2f}",
        "updated_cart": [item.to_dict() for item in current_cart],
        "new_total": round(new_total, 2),
        "budget_ok": budget_ok,
        "split_preview": split_preview,
        "warnings": warnings,
        "fallback": fallback,
    }


async def modify_cart_and_replan(
    session_id: str,
    user_id: str,
    user_instruction: str,
    catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """端到端改单：解析 → 执行 → 重算。
    
    这是 E3 编排函数的对外接口，由前端直接调用。
    """
    # 1. 解析意图
    session = get_or_create_session(session_id, user_id)
    current_cart = session.get_latest_cart()
    
    if not current_cart:
        return {
            "success": False,
            "message": "购物车为空，无法修改",
            "updated_cart": [],
            "new_total": 0,
            "budget_ok": False,
            "warnings": ["请先选择方案并添加到购物车"],
        }
    
    parsed = await parse_modification_intent(user_instruction, current_cart, catalog)
    
    # 2. 执行修改
    result = await execute_modifications(
        session_id=session_id,
        user_id=user_id,
        modifications=parsed.modifications,
        catalog=catalog,
        constraints=session.constraints,
    )
    
    # 3. 补充意图解析结果
    result["intent_summary"] = parsed.intent_summary
    result["expected_delta"] = parsed.expected_total_delta
    
    return result