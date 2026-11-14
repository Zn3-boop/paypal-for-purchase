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
from app.services.group_purchase.constraints import Constraint, total_budget_cap
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

    「只要 X / 只要 A 和 B / 只要 2 个 C」这类「只保留点名商品」的句式走**确定性**
    编译（不进模型）：既省一次往返，也避免模型把它降级成空操作。
    其余指令才交给 LLM 结构化提取，失败时降级规则解析。
    """
    keep_only = _keep_only_plan(user_instruction, current_cart)
    if keep_only is not None:
        keep_mods, keep_items = keep_only
        keep_ids = {it.id for it in keep_items}
        keep_label = "、".join(it.name for it in keep_items)
        dropped = [it for it in current_cart if it.id not in keep_ids]
        return CartModificationOutput(
            modifications=keep_mods,
            intent_summary=(
                f"只要「{keep_label}」：保留 {len(keep_items)} 件，删除其余 {len(dropped)} 件"
                if dropped
                else f"购物车已经只有「{keep_label}」，数量按指令对齐"
            ),
            expected_total_delta=round(
                -sum(it.price * it.quantity for it in dropped), 2
            ),
        )

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


# 「只要 X / 只需要 X 和 Y / 只要 2 个 Z」= **只保留点名的那些商品**（可多件、可带数量），
# 其余全部删除。
#
# 为什么必须确定性处理：这句话既不是 delete（没说要删哪件），也不是 swap/add，
# LLM/规则解析都容易只回一个 adjust_constraint → 执行 0 个改动 → 前端报
# 「没识别到可执行的改动」。但用户意图（"我只要这几件/这两件各两个"）其实非常明确。
# 电商里这是最高频的下单说法，不能靠模型随手发挥。
_KEEP_TRIGGER = re.compile(
    r"(?:只需要?|只要|只买|只想买|仅要|仅买|只留|留下|保留|就要|只想保留|"
    r"only\s+(?:want|need|keep)|keep\s+only)",
    re.IGNORECASE,
)

# 数量单位：中文量词 + 英文 pcs/pieces/units
_QTY_UNIT = r"(?:个|件|副|只|套|台|部|条|盒|瓶|支|对|双|张|包|pcs|pieces|piece|units|unit)"
_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_NUM_TOKEN = r"(?:\d+|[一二两俩三四五六七八九十])"


def _is_keep_only(instruction: str) -> bool:
    return bool(_KEEP_TRIGGER.search(instruction or ""))


def _keep_clause(instruction: str) -> str:
    """取**最后一个**「只要」之后的那段话 —— 保留清单只在这段里找。

    「不要 AVENTHO 300，只要 DJ 300」必须只保留 DJ 300：若拿整句去匹配，
    AVENTHO 也会被当成"要保留"，等于把用户明确否掉的东西留下。
    """
    instruction = instruction or ""
    triggles = list(_KEEP_TRIGGER.finditer(instruction))
    if not triggles:
        return instruction
    return instruction[triggles[-1].end():]


def _to_quantity(token: str) -> int | None:
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token)


def _match_items_in_text(
    text: str,
    cart: list[PlanItem],
) -> list[tuple[PlanItem, int, int]]:
    """在文本里确定性地找出**所有**被点名的购物车商品 → [(商品, 起, 止)]。

    与 `_match_item_in_text`（只取最优一件）的区别：这里要的是集合 ——
    「只要 A 和 B」「只要 A、B、C」必须一次保留多件。
    两级匹配与单件版一致：整名/ID 逐字包含（名字长的先占位，避免 "HD 300"
    抢掉 "HD 300 PROtect"）→ token 覆盖率 ≥0.6 兜底缩写说法。
    """
    lower = (text or "").lower()
    if not lower or not cart:
        return []

    spans: list[tuple[int, int]] = []
    hits: list[tuple[PlanItem, int, int]] = []
    seen: set[str] = set()

    # ① 整名 / ID：长的先占位，避免子串误命中
    cands: list[tuple[int, PlanItem, str]] = []
    for it in cart:
        for key in (it.id, it.name):
            k = (key or "").strip().lower()
            if len(k) >= 3:
                cands.append((len(k), it, k))
    for _, it, key in sorted(cands, key=lambda c: -c[0]):
        start = lower.find(key)
        if start < 0:
            continue
        end = start + len(key)
        if any(not (end <= s or start >= e) for s, e in spans):
            continue  # 已被更长的商品名占位
        spans.append((start, end))
        if it.id not in seen:
            seen.add(it.id)
            hits.append((it, start, end))

    # ② token 覆盖兜底：只补没命中过的件，避免同一件被算两次
    hit_names = [h[0].name.strip().lower() for h in hits if h[0].name]
    for it in cart:
        if it.id in seen:
            continue
        lname = (it.name or "").strip().lower()
        # 已被更精确的点名覆盖：说「AVENTHO 300」不该把
        # 「AVENTHO 300 Wireless Bluetooth Headphones」也一起留下
        if lname and any(h and (h in lname or lname in h) for h in hit_names):
            continue
        tokens = _name_tokens(it.name)
        if not tokens:
            continue
        matched = [t for t in tokens if t in lower]
        if len(matched) / len(tokens) < 0.6:
            continue
        pos = min(lower.find(t) for t in matched)
        seen.add(it.id)
        hits.append((it, pos, pos + 1))

    hits.sort(key=lambda h: h[1])
    return hits


def _nearby_quantity(text: str, start: int, end: int) -> int | None:
    """读商品名紧邻的数量（"2 个 X" / "X 两件" / "X x2" / "X 要 3 个"），读不到返回 None。"""
    before = text[max(0, start - 16):start]
    after = text[end:end + 16]

    m = re.search(
        rf"(?:数量|个数|要|需要)?\s*({_NUM_TOKEN})\s*{_QTY_UNIT}\s*$",
        before,
        re.IGNORECASE,
    )
    if m:
        return _to_quantity(m.group(1))
    m = re.match(rf"\s*[×xX]\s*({_NUM_TOKEN})(?![0-9a-zA-Z])", after)
    if m:
        return _to_quantity(m.group(1))
    m = re.match(rf"\s*(?:要|需要|来|买|改成|换成)?\s*({_NUM_TOKEN})\s*{_QTY_UNIT}", after, re.IGNORECASE)
    if m:
        return _to_quantity(m.group(1))
    return None


def _keep_only_plan(
    instruction: str,
    cart: list[PlanItem],
) -> tuple[list[CartModificationRequest], list[PlanItem]] | None:
    """把「只要 X（可多件 / 可带数量）」编译成一组确定性的购物车指令。

    返回 (指令列表, 要保留的商品)。以下情况返回 None（宁可让用户改口，也不猜）：
      - 不是「只要 …」句式；
      - 购物车为空；
      - 保留清单里一件都认不出来（否则可能把车删空）。
    指令命中、且购物车已经正好只剩这些件时，指令列表为空 —— 由调用方明说「已满足」。
    """
    if not cart or not _is_keep_only(instruction):
        return None

    clause = _keep_clause(instruction)
    matched = _match_items_in_text(clause, cart)
    if not matched:
        return None

    keep_items = [it for it, _, _ in matched]
    keep_ids = {it.id for it in keep_items}
    keep_label = "、".join(it.name for it in keep_items)

    mods: list[CartModificationRequest] = []
    # 「一种多个」：点名商品的期望数量与原数量不一致 → set_quantity（同 id 多行会被合并）
    for it, start, end in matched:
        want = _nearby_quantity(clause, start, end)
        if want is not None and want != it.quantity:
            mods.append(
                CartModificationRequest(
                    modification_type="set_quantity",
                    target_item_id=it.id,
                    new_quantity=want,
                    explanation=f"「只要 {it.name}」→ 数量设为 {want}",
                )
            )
    # 「多个不同种类」：未点名的每一件都删（按 id 删，覆盖同款重复行）
    for it in cart:
        if it.id in keep_ids:
            continue
        mods.append(
            CartModificationRequest(
                modification_type="delete_item",
                target_item_id=it.id,
                explanation=f"「只要 {keep_label}」→ 删除其余商品：{it.name}",
            )
        )

    return mods, keep_items


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
        elif mod.modification_type == "set_quantity":
            if mod.target_item_id and mod.new_quantity:
                new_cart: list[PlanItem] = []
                hit_index: int | None = None
                merged = 0
                for it in current_cart:
                    if it.id == mod.target_item_id:
                        if hit_index is None:
                            # 同一商品可能因重复加购出现多行：只留一行，数量按指令设
                            it.quantity = int(mod.new_quantity)
                            it.subtotal = round(it.price * it.quantity, 2)
                            hit_index = len(new_cart)
                            new_cart.append(it)
                        else:
                            merged += 1
                    else:
                        new_cart.append(it)
                if hit_index is not None:
                    current_cart = new_cart
                    applied += 1 + merged
                else:
                    unmet_targets.append(mod.target_item_id)
            else:
                unmet_targets.append("（没指明要改哪件/改成几个）")
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
    
    # 预算检查（只认组合总价上限：单件预算不能当购物车总额门槛）
    budget_limit = total_budget_cap(session.constraints)
    
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

    # 「只要 X」但一件都认不出来：明确告诉用户可以点什么，而不是"没识别到改动"
    keep_only = _keep_only_plan(user_instruction, current_cart)
    if _is_keep_only(user_instruction) and keep_only is None:
        names = "、".join(it.name for it in current_cart[:4])
        return {
            "success": False,
            "message": f"没能从这句话里认出购物车里的商品。请带上完整商品名；购物车现有：{names}",
            "updated_cart": [it.to_dict() for it in current_cart],
            "new_total": round(
                calc_total([{"price": it.price, "quantity": it.quantity} for it in current_cart]), 2
            ),
            "budget_ok": True,
            "warnings": [],
            "fallback": None,
            "intent_summary": "「只要 X」未匹配到购物车商品",
        }

    parsed = await parse_modification_intent(user_instruction, current_cart, catalog)
    # 确定性补目标：LLM 只回类型不回 id 时，按商品名/ID 在指令里把 target 找回来，
    # 否则 delete_item 会变成空操作（总价不变却报"已改单"）。
    parsed = _repair_targets(parsed, user_instruction, current_cart)

    # 「只要 X」且购物车已经正好只剩这些件（数量也对得上）：本来就满足，
    # 不能再报"没有识别到可执行的改动"，否则用户会以为自己说错了。
    if keep_only is not None and not parsed.modifications:
        total = round(
            calc_total([{"price": it.price, "quantity": it.quantity} for it in current_cart]), 2
        )
        kept_label = "、".join(it.name for it in keep_only[1])
        return {
            "success": True,
            "message": f"购物车已经是「{kept_label}」（{len(current_cart)} 件，${total:.2f}），可直接确认下单",
            "updated_cart": [it.to_dict() for it in current_cart],
            "new_total": total,
            "budget_ok": True,
            "warnings": [],
            "fallback": None,
            "intent_summary": f"购物车已满足「只要 {kept_label}」",
            "expected_delta": 0.0,
        }
    
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