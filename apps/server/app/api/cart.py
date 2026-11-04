"""Shopping Cart Modification APIs - Full Implementation."""
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.group_purchase.session_state import get_session, update_session, PlanItem
from app.services.group_purchase.split_bill import split_bill
from app.services.group_purchase.cart_modifier import modify_cart_and_replan
from app.services.shopper.catalog_k1 import get_k1_items

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cart", tags=["cart"])


class ModifyRequest(BaseModel):
    instruction: str


@router.post("/select_plan")
async def select_plan(session_id: str = Query(...), plan_id: str = Query(...)):
    """将指定方案 A/B 加入购物车作为初始购物车。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if plan_id not in ("A", "B"):
        raise HTTPException(status_code=400, detail="Invalid plan_id, must be A or B")
    
    source_plan = session.plan_a if plan_id == "A" else session.plan_b
    if not source_plan:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not generated yet")
    
    session.selected_plan = source_plan
    session.cart_items = [PlanItem.from_dict(item.to_dict()) for item in source_plan.items]
    update_session(session)
    
    return {
        "ok": True,
        "plan_id": plan_id,
        "items_count": len(session.cart_items),
        "total": round(sum(item.price * item.quantity for item in session.cart_items), 2),
    }


@router.get("")
async def get_cart(session_id: str = Query(...)):
    """获取当前购物车。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    items = session.get_latest_cart()
    total = sum(item.price * item.quantity for item in items)
    
    return {
        "items": [item.to_dict() for item in items],
        "total": round(total, 2),
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