"""订单档案 API（H2/H3 模块）。

Endpoints（H2 四个就够）：
  - GET  /api/v1/orders?session_id=...   → 档案页列表
  - GET  /api/v1/orders/{order_id}       → 详情（状态 + 金额 + 商品快照 + 关联审计事件）
  - GET  /api/v1/orders/{order_id}/status→ 轻量轮询（状态 + 转移轨迹）
  - POST /api/v1/orders/{order_id}/cancel→ 取消订单（paid 之前可取消）

H4 提示（批 3）：order_id 查询接口设计成可独立调用，自然语言查单（query_order）
直接复用 GET /orders/{order_id}/status，Agent 无需关心状态机内部。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.group_purchase.audit_log import get_audit_logger
from app.services.group_purchase.order_query import answer_order_query, is_order_query
from app.services.group_purchase.order_lifecycle import (
    OrderStatus,
    cancel_order,
    get_order_record,
    list_order_records,
)

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
async def list_orders(session_id: str = Query(..., description="按会话过滤订单档案")):
    """订单档案列表（H3 档案页数据源）；session_id 必填。"""
    records = list_order_records(session_id=session_id)
    return {
        "orders": [r.brief() for r in records],
        "count": len(records),
    }


class OrderQueryRequest(BaseModel):
    text: str


@router.post("/query")
async def query_orders(payload: OrderQueryRequest, session_id: str = Query(...)):
    """H4 自然语言查单：只读；session_id 由服务端/Agent 注入，强制本会话归属。

    请求体只接受自然语言 `text`（不接受 order_id 越权参数）；显式单号也必须属于该会话。
    """
    result = answer_order_query(session_id, payload.text)
    result["intent"] = "query_order" if is_order_query(payload.text) else "unknown"
    return result


@router.get("/{order_id}")
async def get_order_detail(order_id: str, session_id: str = Query(...)):
    """订单详情：状态 + 金额 + 商品快照 + 关联审计事件。"""
    order = get_order_record(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"订单不存在：{order_id}")
    if order.session_id != session_id:
        raise HTTPException(status_code=403, detail="无权访问该订单")

    detail = order.to_dict()

    # 关联该订单的审计事件：order_status_changed，或 data.order_id 指向本订单
    try:
        logs = get_audit_logger().get_session_timeline(order.session_id)
        related = [
            entry.to_dict()
            for entry in logs
            if entry.event == "order_status_changed"
            or str(entry.data.get("order_id", "")) == order.order_id
        ]
        related.sort(key=lambda e: e.get("timestamp", ""))
        detail["audit_events"] = related
        detail["audit_count"] = len(related)
    except Exception as exc:  # pragma: no cover - 审计查询失败不阻断详情
        detail["audit_events"] = []
        detail["audit_error"] = str(exc)

    return detail


@router.get("/{order_id}/status")
async def get_order_status(order_id: str, session_id: str = Query(...)):
    """轻量轮询：status + updated_at + 转移轨迹（H4 自然语言查单直接复用）。"""
    order = get_order_record(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"订单不存在：{order_id}")
    if order.session_id != session_id:
        raise HTTPException(status_code=403, detail="无权访问该订单")
    return {
        "order_id": order.order_id,
        "session_id": order.session_id,
        "status": order.status.value,
        "total": order.total,
        "currency": order.currency,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "cancelled_reason": order.cancelled_reason,
        "transitions": order.transitions,
    }


@router.post("/{order_id}/cancel")
async def cancel_existing_order(
    order_id: str,
    session_id: str = Query(...),
    reason: str = "用户取消",
):
    """取消订单：仅 draft / confirmed / paying 可取消；paid 之后拒绝。"""
    order = get_order_record(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"订单不存在：{order_id}")
    if order.session_id != session_id:
        raise HTTPException(status_code=403, detail="无权操作该订单")

    if order.status == OrderStatus.PAID:
        raise HTTPException(status_code=409, detail="订单已支付，无法取消")
    if order.status == OrderStatus.CANCELLED:
        return {
            "ok": True,
            "order_id": order.order_id,
            "status": "cancelled",
            "idempotent": True,
            "message": "订单已处于取消状态",
        }

    ok, msg = cancel_order(order, actor="user", reason=reason)
    return {
        "ok": ok,
        "order_id": order.order_id,
        "status": order.status.value,
        "message": msg,
    }