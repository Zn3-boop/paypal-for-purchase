"""Shopping Cart Modification APIs - Full Implementation."""
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.group_purchase.session_state import get_session, update_session, PlanItem
from app.services.group_purchase.split_bill import split_bill
from app.services.group_purchase.cart_modifier import modify_cart_and_replan
from app.services.group_purchase.budget_guardrail import calc_total, check_budget
from app.services.shopper.catalog_k1 import get_k1_items

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cart", tags=["cart"])


class ModifyRequest(BaseModel):
    instruction: str


class QuantityUpdate(BaseModel):
    """PATCH /cart/items/{id} 的请求体：只改数量，不改价、不换货。"""

    quantity: int


def _hard_budget(session) -> float | None:
    """当前会话的组合总价上限（多成员取最小值）；单件预算不算总额门槛。"""
    from app.services.group_purchase.constraints import total_budget_cap

    return total_budget_cap(session.constraints)


def _cart_body(session) -> dict:
    items = session.get_latest_cart()
    payload = [i.to_dict() for i in items]
    return {"items": payload, "total": round(calc_total(payload), 2)}


def _guardrail(items: list[PlanItem], budget: float | None) -> dict:
    """只**报告**不拦截：购物车是用户可见、可改的中间态。

    唯一的把关点是支付时刻（`/paypal/create_order` 按购物车重算 + 预算守卫）。
    这里若拦截，用户连"先改到预算内再付"都做不到 —— 那正是「预算拦截后无法继续」
    的老问题。所以 PATCH/DELETE 允许改，但把超支事实写进响应让前端提示。
    """
    payload = [i.to_dict() for i in items]
    total = round(calc_total(payload), 2)
    if budget is None:
        return {
            "status": "no_budget",
            "ok": True,
            "budget": None,
            "total": total,
            "overage": None,
            "message": "会话里没有硬预算约束，本次未做预算复核。",
        }
    checked = check_budget(payload, budget, allow_substitute=False)
    return {
        "status": "ok" if checked["ok"] else "fail",
        "ok": bool(checked["ok"]),
        "budget": budget,
        "total": checked["total"],
        "overage": checked["overage"],
        "message": checked["resolution"],
    }


def _audit_cart_change(session, modification_type: str, item_ids: list[str],
                       old_total: float, new_total: float) -> None:
    """购物车任何可见改动都留痕（复用既有 cart_modified 事件，不新增事件类型）。"""
    try:
        from app.services.group_purchase.audit_log import log_cart_modification

        log_cart_modification(
            session_id=session.session_id,
            user_id=getattr(session, "user_id", "") or "demo-user",
            actor="cart_api",
            modification_type=modification_type,
            item_ids=item_ids,
            old_total=old_total,
            new_total=new_total,
        )
    except Exception:  # pragma: no cover - 审计失败不影响主链路
        logger.warning("cart audit log failed", exc_info=True)


@router.post("/select_plan")
async def select_plan(
    session_id: str = Query(...),
    plan_id: str = Query(...),
    mode: str = Query("replace", description="replace=用方案快照替换购物车；append=按商品 id 合并数量"),
):
    """把方案 A/B 放进购物车 —— 语义显式化。

    为什么必须显式化：购物车现在是**用户可见、可改**的中间态（PATCH/DELETE + 面板），
    而旧实现无条件 `cart_items = 方案快照`。于是「手动加购/改数量 → 再点另一个方案」
    会把用户的改动静默清空 —— 这是录制现场必炸的坑。所以：

      - `replace`（默认，也是首次选方案的语义）：购物车 ← 方案快照；
        原有商品被移除时写 `cart_modified`，响应回显 `removed_count`；
      - `append`：按商品 id 合并数量（已有商品数量相加、新商品追加），
        同样写 `cart_modified`，响应回显 `added_count`。

    前端据 `replaced / removed_count / added_count` 提示「已合并 N 件 / 已替换 N 件」，
    不再有"改了没生效"或"改动莫名消失"两种可能。
    """
    if mode not in ("replace", "append"):
        raise HTTPException(status_code=400, detail="mode must be 'replace' or 'append'")

    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if plan_id not in ("A", "B"):
        raise HTTPException(status_code=400, detail="Invalid plan_id, must be A or B")

    source_plan = session.plan_a if plan_id == "A" else session.plan_b
    if not source_plan:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not generated yet")

    before = session.get_latest_cart()
    before_ids = {i.id for i in before}
    old_total = round(calc_total([i.to_dict() for i in before]), 2)

    session.selected_plan = source_plan

    if mode == "append":
        merged = {i.id: i for i in before}
        added_count = 0
        for item in source_plan.items:
            existing = merged.get(item.id)
            if existing is not None:
                existing.quantity = int(existing.quantity or 1) + int(item.quantity or 1)
                existing.subtotal = round(existing.price * existing.quantity, 2)
            else:
                clone = PlanItem.from_dict(item.to_dict())
                merged[clone.id] = clone
                added_count += 1
        session.cart_items = list(merged.values())
        removed_count = 0
    else:
        session.cart_items = [PlanItem.from_dict(item.to_dict()) for item in source_plan.items]
        plan_ids = {item.id for item in session.cart_items}
        removed_count = len([i for i in before if i.id not in plan_ids])
        added_count = len([i for i in session.cart_items if i.id not in before_ids])

    new_total = round(calc_total([i.to_dict() for i in session.cart_items]), 2)
    update_session(session)

    # 只有真的动过旧内容才留痕（首次选方案 = 初始化，不必制造噪声）
    if before:
        _audit_cart_change(
            session,
            f"select_plan_{mode}",
            [i.id for i in session.cart_items],
            old_total,
            new_total,
        )

    # 选择记忆：记录「选过哪个方案」，供下次用同一个检索词再看一次（只做回显）。
    try:
        from app.services.group_purchase.search_memory import record_selection

        record_selection(
            session_id=session_id,
            user_id=getattr(session, "user_id", "") or "demo-user",
            plan_id=plan_id,
            total=new_total,
            item_names=[item.name for item in session.cart_items],
            query=getattr(session, "intent_query", "") or "",
        )
    except Exception:
        logger.debug("selection memory record failed", exc_info=True)

    body = _cart_body(session)
    return {
        "ok": True,
        "plan_id": plan_id,
        "mode": mode,
        "replaced": mode == "replace" and bool(before),
        "removed_count": removed_count,
        "added_count": added_count,
        "items_count": len(body["items"]),
        "total": body["total"],
        "old_total": old_total,
        "cart": body,
        "guardrail": _guardrail(session.get_latest_cart(), _hard_budget(session)),
    }


@router.get("")
async def get_cart(session_id: str = Query(...)):
    """获取当前购物车（含预算合规事实：只报告不拦截，把关点在支付时刻）。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    items = session.get_latest_cart()
    total = sum(item.price * item.quantity for item in items)
    
    return {
        "items": [item.to_dict() for item in items],
        "total": round(total, 2),
        "guardrail": _guardrail(items, _hard_budget(session)),
    }


@router.post("/split_preview")
async def split_preview(session_id: str = Query(...)):
    """生成分摊预览。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    items = session.get_latest_cart()
    if not items:
        raise HTTPException(status_code=400, detail="Cart is empty")
    
    total = sum(item.price * item.quantity for item in items)
    
    # 从约束中提取成员
    member_ids = [c.source for c in session.constraints if c.source and c.status == "active"]
    if len(member_ids) < 2:
        member_ids = ["成员 A", "成员 B", "成员 C", "成员 D", "成员 E"]
    
    result = split_bill(total=total, members=member_ids)
    session.split_preview = result
    update_session(session)
    
    return result.to_dict()


@router.post("/modify")
async def modify_cart(session_id: str = Query(...), req: ModifyRequest = None):
    """根据自然语言指令修改购物车（完整 LLM 驱动）。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    instruction = req.instruction if req else ""
    if not instruction.strip():
        items = session.get_latest_cart()
        total = sum(item.price * item.quantity for item in items)
        return {
            "success": False,
            "message": "指令为空",
            "cart": {"items": [i.to_dict() for i in items], "total": round(total, 2)},
        }
    
    current_cart = session.get_latest_cart()
    if not current_cart:
        return {
            "success": False,
            "message": "购物车为空",
            "cart": {"items": [], "total": 0},
            "fallback": "请先选择方案并添加商品。",
        }
    
    try:
        catalog = get_k1_items()
        result = await modify_cart_and_replan(
            session_id=session_id,
            user_id=session.user_id or "demo-user",
            user_instruction=instruction,
            catalog=catalog,
        )
        
        updated_cart = result.get("updated_cart", [])
        new_total = result.get("new_total", 0)
        
        guardrail_status = "ok" if result.get("budget_ok", True) else "fail"
        guardrail_message = None if guardrail_status == "ok" else result.get("message")
        
        return {
            "success": result.get("success", False),
            "diff": {
                "changed_items": updated_cart,
                "old_total": round(sum(i.price * i.quantity for i in current_cart), 2),
                "new_total": round(new_total, 2),
            },
            "guardrail": {
                "status": guardrail_status,
                "message": guardrail_message,
            },
            "cart": {
                "items": updated_cart,
                "total": round(new_total, 2),
            },
            "fallback": result.get("fallback"),
            "split_preview": result.get("split_preview"),
            "intent_summary": result.get("intent_summary"),
            "warnings": result.get("warnings", []),
        }
    
    except Exception as e:
        logger.exception("Cart modification failed: %s", e)
        raise HTTPException(status_code=500, detail=f"修改失败：{e}")


@router.post("/items/{item_id}")
async def add_cart_item(
    item_id: str,
    session_id: str = Query(...),
):
    """把方案快照里的**某一单品**加进购物车 —— 单选用户的出口。

    为什么需要它：`select_plan` 是整份方案入车（replace/append），对「我只想买一件」的
    用户意味着先装进 8~16 件再逐个删 —— 录屏/真实使用都抱怨过。这里提供按件加购：
      - 商品必须来自**当前会话的方案快照**（selected_plan / plan_a / plan_b），
        不允许前端伪造任意商品（价格、来源都由快照决定，购物车不藏假货）；
      - 已在购物车 → 数量 +1；不在 → 以方案快照为准克隆入车；
      - 写 cart_modified（add_item）审计，返回真实购物车 + guardrail 事实。
    """
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # 从方案快照里定位该商品（按优先级 selected_plan → plan_a → plan_b）
    source_item: PlanItem | None = None
    for plan in (session.selected_plan, session.plan_a, session.plan_b):
        for it in (plan.items if plan else []):
            if it.id == item_id:
                source_item = it
                break
        if source_item is not None:
            break
    if source_item is None:
        raise HTTPException(
            status_code=404,
            detail=f"方案快照里没有商品 {item_id}（只能从方案里按件加购，防止伪造商品/价格）",
        )

    items = list(session.cart_items)
    old_total = round(calc_total([i.to_dict() for i in items]), 2)

    existing = next((i for i in items if i.id == item_id), None)
    if existing is not None:
        existing.quantity = int(existing.quantity or 1) + 1
        existing.subtotal = round(existing.price * existing.quantity, 2)
    else:
        clone = PlanItem.from_dict(source_item.to_dict())
        clone.quantity = 1
        clone.subtotal = round(clone.price, 2)
        items.append(clone)
    session.cart_items = items
    update_session(session)
    session.save_snapshot(reason="cart_item_added")

    new_total = round(calc_total([i.to_dict() for i in session.cart_items]), 2)
    _audit_cart_change(session, "add_item", [item_id], old_total, new_total)

    body = _cart_body(session)
    return {
        "success": True,
        "message": f"已加入 {source_item.name} × 1（购物车 ${new_total:.2f}）",
        "item_id": item_id,
        "quantity": existing.quantity if existing is not None else 1,
        "old_total": old_total,
        "new_total": new_total,
        "cart": body,
        "guardrail": _guardrail(session.get_latest_cart(), _hard_budget(session)),
    }


@router.patch("/items/{item_id}")
async def update_cart_item(
    item_id: str,
    req: QuantityUpdate,
    session_id: str = Query(...),
):
    """改购物车里某一行的数量（面板上的 ± 按钮）。

    规则：
      - `quantity` 必须 ≥ 1；要移除请用 DELETE —— 允许 0 会让"手滑减到 0"静默丢件；
      - 同一 `item_id` 存在多行时合并成一行（同款拆两行是数据噪声，不是用户意图）；
      - **不拦截超预算**：只把 `guardrail` 事实回报给前端。用户需要能"改到预算内再付"，
        真正把门的是 `/paypal/create_order`。
    """
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if req.quantity < 1:
        raise HTTPException(
            status_code=400,
            detail="quantity 必须 ≥ 1；要移除商品请调用 DELETE /cart/items/{item_id}",
        )

    items = list(session.cart_items)
    targets = [i for i in items if i.id == item_id]
    if not targets:
        raise HTTPException(status_code=404, detail=f"购物车里没有商品 {item_id}")

    old_total = round(calc_total([i.to_dict() for i in items]), 2)

    keeper = targets[0]
    keeper.quantity = int(req.quantity)
    keeper.subtotal = round(keeper.price * keeper.quantity, 2)
    # 保留 keeper，其余同 id 行丢弃（数量已在 keeper 上）
    session.cart_items = [i for i in items if i is keeper or i.id != item_id]
    update_session(session)
    session.save_snapshot(reason="cart_quantity_updated")

    new_total = round(calc_total([i.to_dict() for i in session.cart_items]), 2)
    _audit_cart_change(session, "update_quantity", [item_id], old_total, new_total)

    return {
        "success": True,
        "message": f"{keeper.name} × {keeper.quantity} = ${keeper.subtotal:.2f}",
        "item_id": item_id,
        "quantity": keeper.quantity,
        "old_total": old_total,
        "new_total": new_total,
        "cart": _cart_body(session),
        "guardrail": _guardrail(session.get_latest_cart(), _hard_budget(session)),
    }


@router.delete("/items/{item_id}")
async def delete_cart_item(
    item_id: str,
    session_id: str = Query(...),
):
    """从购物车移除商品（面板上的 🗑）。同 id 多行一并移除。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    items = list(session.cart_items)
    if not any(i.id == item_id for i in items):
        raise HTTPException(status_code=404, detail=f"购物车里没有商品 {item_id}")

    removed = [i for i in items if i.id == item_id]
    removed_name = removed[0].name if removed else item_id
    old_total = round(calc_total([i.to_dict() for i in items]), 2)

    session.cart_items = [i for i in items if i.id != item_id]
    update_session(session)
    session.save_snapshot(reason="cart_item_removed")

    new_total = round(calc_total([i.to_dict() for i in session.cart_items]), 2)
    _audit_cart_change(session, "remove_item", [item_id], old_total, new_total)

    return {
        "success": True,
        "message": f"已从购物车移除 {removed_name}",
        "item_id": item_id,
        "remove_count": len(removed),
        "old_total": old_total,
        "new_total": new_total,
        "cart": _cart_body(session),
        "guardrail": _guardrail(session.get_latest_cart(), _hard_budget(session)),
    }