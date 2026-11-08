"""Session Management APIs for Group Purchase."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.group_purchase.session_state import (
    get_or_create_session,
    get_session,
    update_session,
)

router = APIRouter(prefix="/session", tags=["session"])


class ResetRequest(BaseModel):
    session_id: str
    user_id: str | None = None
    hard_reset: bool = False  # True = 清空所有约束（包括软约束），False = 只重置购物车 + 审计


class ResetResponse(BaseModel):
    success: bool
    session_id: str
    message: str
    initial_constraints_count: int


@router.post("/reset", response_model=ResetResponse)
async def reset_session(req: ResetRequest) -> ResetResponse:
    """一键重置会话：清空购物车、审计轮次，可选择是否保留约束。

    用途：
      - 录屏 NG 重来（3 秒恢复到初始状态）
      - 前端一键重置按钮（全局 Header）
      - 调试时快速刷新状态

    行为：
      - hard_reset=False（默认）：保留硬约束 + 软约束，清空购物车 + 审计 + split_preview
      - hard_reset=True：保留预算约束，清空所有其他约束 + 购物车 + 审计
    """
    try:
        session = get_session(req.session_id)
        if session is None:
            if req.user_id:
                session = get_or_create_session(req.session_id, req.user_id)
            else:
                raise HTTPException(status_code=404, detail="Session not found")

        if req.hard_reset:
            # 硬重置：保留预算约束，清空其他
            budget_constraints = [c for c in session.constraints if c.type == "budget" and c.status == "active"]
            session.constraints = budget_constraints
            session.cart_items = []
            session.history = []
            session.split_preview = None
        else:
            # 软重置：保留所有约束，只清空购物车 + 审计
            session.cart_items = []
            session.history = []
            session.split_preview = None

        # 快照失效：GET /plans 是"会话内首次生成"的快照，reset 是唯一的重算入口。
        # 不在这里清掉的话，录屏"NG 重来"会直接回放上一轮的方案；
        # 而 A5 边界（删约束 → 重算 → 方案内容变化）也无从验证。
        # 注意：plan_generated 审计写在磁盘 JSONL 上、append-only，reset 清不掉——
        # 要一条干净的审计屏，请换新 session_id（reset 清不掉磁盘上的 append-only 审计）。
        session.plan_a = None
        session.plan_b = None
        session.selected_plan = None
        session.plans_snapshot = None
        if session.state in ("plan_generated", "reviewing", "confirmed", "completed"):
            session.state = "constraint_collection"

        # 解除会话与订单的绑定，并取消仍在流程中的订单。
        # 否则重置后 session.order_id 仍指向旧订单（paid/cancelled），
        # 下一次 create_order 会直接 409「订单已支付/已取消」，且 paying 订单会让档案页无限轮询。
        from app.services.group_purchase.order_lifecycle import (
            OrderStatus,
            cancel_order,
            get_order_record,
        )

        if session.order_id:
            stale = get_order_record(session.order_id)
            if stale is not None and stale.status in (
                OrderStatus.DRAFT,
                OrderStatus.CONFIRMED,
                OrderStatus.PAYING,
            ):
                cancel_order(stale, actor="user", reason="会话重置")
        session.order_id = None
        session.order_status = None

        update_session(session)

        return ResetResponse(
            success=True,
            session_id=session.session_id,
            message="会话已重置" + ("（硬重置）" if req.hard_reset else ""),
            initial_constraints_count=len(session.constraints),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重置失败：{e}")


@router.get("/{session_id}/status")
async def get_session_status(session_id: str) -> dict:
    """获取会话状态概览（用于前端显示重置前的状态）。"""
    session = get_session(session_id)
    if session is None:
        return {
            "exists": False,
            "message": "Session not found",
        }

    return {
        "exists": True,
        "session_id": session.session_id,
        "user_id": session.user_id,
        "constraints_count": len(session.constraints),
        "cart_items_count": len(session.cart_items),
        "audit_log_count": len(session.history),
        "has_split_preview": bool(session.split_preview),
        "budget_limit": next(
            (float(c.numeric_value) for c in session.constraints if c.type == "budget" and c.numeric_value),
            None,
        ),
    }