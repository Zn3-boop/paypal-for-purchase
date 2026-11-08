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
import re
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
        # 用与 LLM 路径同一套确定性匹配（整名 → token 覆盖），不依赖 split()[0]
        hit = _match_item_in_text(instruction, cart)
        if hit is not None:
            modifications.append(CartModificationRequest(
                modification_type="delete_item",
                target_item_id=hit.id,
                explanation=f"检测到删除指令：{instruction}",
            ))
    
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


def _name_tokens(name: str) -> list[str]:
    """把商品名切成可用于匹配的 token（字母/数字/中文，长度 ≥2）。"""
    return [t for t in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", (name or "").lower()) if len(t) >= 2]


def _match_item_in_text(instruction: str, cart: list[PlanItem]) -> PlanItem | None:
    """在指令里**确定性地**找出要改的那件商品。

    为什么需要它：LLM 只是「可能」给出 target_item_id。实测用户直接贴商品名
    （「删掉 54087X-R7 - 5 Man Rod - Blue - 1 Pack」）时，模型经常只回
    modification_type=delete_item 却不带 id；执行阶段于是什么也没删，
    总价变化 +0.00，界面却显示「已改单」——用户看到的就是"价格没减少"。
    这里用「整名/整 ID 包含 → token 覆盖率」两级匹配兜底，不依赖模型发挥。
    """
    text = (instruction or "").lower()
    if not text or not cart:
        return None

    # ① 整名 / 整 ID 出现在指令里（最长优先，避免子串误命中）
    best: PlanItem | None = None
    best_len = 0
    for it in cart:
        for key in (it.id, it.name):
            k = (key or "").strip().lower()
            if len(k) >= 3 and k in text and len(k) > best_len:
                best, best_len = it, len(k)
    if best is not None:
        return best

    # ② token 覆盖率：用户只打了一部分名字时（例如「删掉 5 Man Rod 那件」）
    best_score = 0.0
    for it in cart:
        tokens = _name_tokens(it.name)
        if not tokens:
            continue
        score = sum(1 for t in tokens if t in text) / len(tokens)
        if score > best_score:
            best, best_score = it, score
    return best if best_score >= 0.6 else None


def _repair_targets(
    parsed: CartModificationOutput,
    instruction: str,
    cart: list[PlanItem],
) -> CartModificationOutput:
    """LLM 漏给 / 给错 target_item_id 时，按商品名把目标补回来。"""
    for mod in parsed.modifications:
        if mod.modification_type not in ("delete_item", "swap_item"):
            continue
        if mod.target_item_id and any(it.id == mod.target_item_id for it in cart):
            continue
        hit = _match_item_in_text(instruction, cart)
        if hit is not None:
            mod.target_item_id = hit.id
    return parsed


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
    # 记录"真的改动了什么"：改单最容易翻车的地方是「指令解析到了、执行却是空操作」，
    # 于是总价 +0.00 但界面报"已改单"。这里以副作用为准，没有副作用就不算成功。
    applied: int = 0
    unmet_targets: list[str] = []

    # 依次执行修改
    for mod in modifications:
        if mod.modification_type == "delete_item":
            if mod.target_item_id:
                before = len(current_cart)
                current_cart = [it for it in current_cart if it.id != mod.target_item_id]
                if len(current_cart) < before:
                    applied += 1
                else:
                    unmet_targets.append(mod.target_item_id)
            else:
                unmet_targets.append("（没指明要删哪件）")
        elif mod.modification_type == "swap_item":
            if mod.target_item_id and mod.replacement_item_id:
                # 在 catalog 中找到替换商品
                new_item_dict = next((c for c in catalog if c["id"] == mod.replacement_item_id), None)
                if new_item_dict:
                    # 替换旧商品
                    for i, item in enumerate(current_cart):
                        if item.id == mod.target_item_id:
                            current_cart[i] = PlanItem.from_dict(new_item_dict)
                            applied += 1
                            break
                    else:
                        unmet_targets.append(mod.target_item_id)
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
            applied += 1
        elif mod.modification_type == "adjust_budget":
            if mod.new_constraint_value:
                # 查找预算约束并更新
                for c in session.constraints:
                    if c.type == "budget" and c.hard and c.status == "active":
                        c.numeric_value = float(mod.new_constraint_value)
                        c.status = "active"
                        applied += 1
                        break
        elif mod.modification_type == "adjust_constraint":
            # 通用约束调整
            pass

    for target in unmet_targets:
        warnings.append(f"没在购物车里找到要改的商品：{target}")
    
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
        "success": applied > 0 and budget_ok,
        "message": (
            f"修改完成，总价变化 {delta:+.2f}"
            if applied > 0
            else "没有识别到可执行的改动（没匹配到购物车里的商品）。请带上商品名，例如「删掉 <商品名>」"
        ),
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
    # 确定性补目标：LLM 只回类型不回 id 时，按商品名/ID 在指令里把 target 找回来，
    # 否则 delete_item 会变成空操作（总价不变却报"已改单"）。
    parsed = _repair_targets(parsed, user_instruction, current_cart)
    
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