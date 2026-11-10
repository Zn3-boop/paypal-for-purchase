"""H4 自然语言查单（只读，无支付/改单副作用）。

边界（与 B1 铁律一致）：
  - 查单是**只读**能力：不改购物车、不触碰订单状态机、不发起支付。
  - `session_id` 由服务端注入（Agent 从会话状态取），前端/模型都无法指定别人的会话；
    即使文本里出现别的 `ord_xxx`，只要不属于当前会话就拒绝，绝不泄露跨会话订单。
  - 不依赖 LLM：单号/“最近”等关键词确定性解析，答案直接来自 `order_lifecycle` 状态机事实。

对外入口：`POST /api/v1/orders/query?session_id=...`（见 app/api/orders.py）。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.group_purchase.order_lifecycle import get_order_record, list_order_records

# 触发词：供 Agent 路由判断“这是查单，不是改单/支付”。查询语义永远优先于模糊的“订单”名词。
QUERY_ORDER_TRIGGER = re.compile(
    r"(订单|单号|order|支付(到|状态)|到哪一步|查单|物流|发货|付款了?吗|paid\??)",
    re.IGNORECASE,
)

_ORDER_ID_RE = re.compile(r"\b(ord_[0-9a-zA-Z]+)\b", re.IGNORECASE)
_LATEST_RE = re.compile(r"(最近|最新|最后一|上一|latest|last)", re.IGNORECASE)

STATUS_LABEL: dict[str, str] = {
    "draft": "待复核",
    "confirmed": "已确认",
    "paying": "支付中",
    "paid": "已支付",
    "cancelled": "已取消",
    "expired": "已超时（支付窗口已过）",
}


def is_order_query(text: str) -> bool:
    """是否存在查单意图（供 Agent 路由；真正执行仍走 answer_order_query）。"""
    return bool(QUERY_ORDER_TRIGGER.search(text or ""))


def _mm(value: float, currency: str) -> str:
    return f"${value:.2f}" if currency == "USD" else f"{value:.2f} {currency}"


def _describe(record, *, transitions: int | None = None) -> str:
    status = record.status.value if hasattr(record.status, "value") else str(record.status)
    label = STATUS_LABEL.get(status, status)
    parts = [
        f"订单 {record.order_id} 当前状态：{label}（{status}）",
        f"金额 {_mm(record.total, record.currency)}",
    ]
    if record.plan_id:
        parts.append(f"方案 {record.plan_id}")
    if record.cancelled_reason:
        parts.append(f"取消原因：{record.cancelled_reason}")
    if transitions is not None:
        parts.append(f"已记录 {transitions} 次状态转移")
    parts.append("最终支付确认始终由你在 PayPal 页面完成，AI 不会自行发起支付。")
    return "，".join(parts[:-1]) + "。" + parts[-1]


def _not_found(reason: str, reply: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "reply": reply, "orders": []}


def answer_order_query(session_id: str, text: str) -> dict[str, Any]:
    """解析一句自然语言并返回本会话订单事实。

    返回结构给前端和 Agent 共用：
      {ok, reason?, ambiguous?, order_id?, status?, status_label?, total?, currency?,
       plan_id?, updated_at?, transitions?, reply, orders}
    """
    if not session_id:
        return _not_found("missing_session", "缺少会话上下文，无法查询订单。请先进入团购会话。")

    # 由服务端按会话取单，先保证“只能看到自己的订单”。
    orders = list_order_records(session_id=session_id)
    if not orders:
        return _not_found(
            "no_orders",
            "当前会话还没有团购订单。完成方案复核并支付后，订单会出现在订单档案里。",
        )

    briefs = [record.brief() for record in orders]
    match = _ORDER_ID_RE.search(text or "")
    if match:
        order_id = match.group(1)
        target = get_order_record(order_id)
        # 归属校验：存在但不属于本会话时统一回“没有找到”，不暴露该单是否存在。
        if target is None or target.session_id != session_id:
            return _not_found("order_not_found", "没有找到该订单（只能查询本会话的订单）。")
        status = target.status.value
        return {
            "ok": True,
            "ambiguous": False,
            "order_id": target.order_id,
            "status": status,
            "status_label": STATUS_LABEL.get(status, status),
            "total": target.total,
            "currency": target.currency,
            "plan_id": target.plan_id,
            "updated_at": target.updated_at,
            "transitions": len(target.transitions),
            "reply": _describe(target, transitions=len(target.transitions)),
            "orders": briefs,
        }

    latest = orders[0]  # list_order_records 已按 created_at 倒序
    if len(orders) > 1 and not _LATEST_RE.search(text or ""):
        lines = [
            f"· {record.order_id}：{STATUS_LABEL.get(record.status.value, record.status.value)}"
            f"（{_mm(record.total, record.currency)}）"
            for record in orders[:5]
        ]
        return {
            "ok": True,
            "ambiguous": True,
            "reply": (
                f"本会话有 {len(orders)} 个订单，请带上订单号查询具体一单，或说“最近的订单”。\n"
                + "\n".join(lines)
            ),
            "orders": briefs,
        }

    status = latest.status.value
    return {
        "ok": True,
        "ambiguous": False,
        "order_id": latest.order_id,
        "status": status,
        "status_label": STATUS_LABEL.get(status, status),
        "total": latest.total,
        "currency": latest.currency,
        "plan_id": latest.plan_id,
        "updated_at": latest.updated_at,
        "transitions": len(latest.transitions),
        "reply": _describe(latest, transitions=len(latest.transitions)),
        "orders": briefs,
    }