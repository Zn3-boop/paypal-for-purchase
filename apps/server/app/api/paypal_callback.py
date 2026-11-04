"""PayPal Callback Endpoints for G3 Complete Payment Flow.

PayPal 沙盒流程闭环：
1. 创建订单 -> 返回 approval_url
2. 用户点击 approval_url -> PayPal 批准页面
3. User approves -> PayPal redirects to return_url with token
4. Backend captures order -> status COMPLETED
5. Redirect to frontend with success

Endpoints:
- POST /api/v1/paypal/create_order -> 创建并返回 approval_url
- GET  /api/v1/paypal/callback?token={token}&PayerID={payer_id} -> 处理 PayPal 回调，捕获订单
- GET  /api/v1/paypal/cancel -> 处理用户取消

关键设计（2026-10-31 修复）：
  - session_id 透传：PayPal 回跳只会带回 token/PayerID，不会带业务字段。
    create_order 时把 session_id 拼进 return_url，同时落一份进程内 order_id 映射，
    回调侧双路兜底读取，避免审计链路断在 session_id=None。
  - 审计落地：capture 成功后写 payment_completed 审计事件（此前该函数无调用点，
    种子脚本承诺的审计记录永远不会出现）。
  - 浏览器跳转：Accept 含 text/html 时返回 302 到前端落地页；脚本/curl 调用
    仍返回原 JSON，两种用法互不影响。

P0 HITL 修复 2026-10-31：create_order 不信任前端传入的 amount，强制使用 session 购物车总价。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.services.paypal_gateway import get_paypal_gateway
from app.services.group_purchase.session_state import get_session, update_session

router = APIRouter(prefix="/paypal", tags=["paypal"])
logger = logging.getLogger(__name__)

# order_id -> 业务上下文（进程内映射，演示级）
# PayPal 回跳只带 token，业务侧的 session_id / user_email 靠这张表还原。
_PENDING_ORDERS: dict[str, dict[str, Any]] = {}

class CreateOrderRequest(BaseModel):
    amount: float  # 兼容旧客户端，不再信任该字段
    session_id: str
    user_email: str
    item_name: str = "ECV Group Purchase"


class CreateOrderResponse(BaseModel):
    order_id: str
    approval_url: str
    status: str
    message: str = "请前往 PayPal 批准订单"
    idempotent: bool = False


def _with_session(base_url: str, session_id: str) -> str:
    """把 session_id 拼进回跳 URL 的 query，让 PayPal 原样带回。"""
    if not session_id:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{urlencode({'session_id': session_id})}"


def _frontend_base() -> str:
    return (get_settings().frontend_base_url or "http://localhost:3000").rstrip("/")


def _wants_html(request: Request) -> bool:
    """浏览器（Accept 含 text/html）需要 302 跳转；脚本调用保持 JSON。"""
    return "text/html" in (request.headers.get("accept") or "").lower()


def _log_payment_completed(
    *, order_id: str, session_id: str, user_id: str, amount: float
) -> None:
    """写 payment_completed 审计事件（失败不阻断支付主链路）。"""
    try:
        from app.services.group_purchase.audit_log import log_payment_completion

        log_payment_completion(
            session_id=session_id or "unknown",
            user_id=user_id or "demo-user",
            actor="paypal_callback",
            order_id=order_id,
            payment_method="paypal_sandbox",
            final_total=float(amount),
            split_details=[],
        )
        logger.info(
            "audit payment_completed written: order_id=%s session_id=%s user=%s amount=%.2f",
            order_id, session_id, user_id, amount,
        )
    except Exception as exc:  # pragma: no cover - 审计失败不阻断支付
        logger.warning("audit payment_completed failed: %s", exc)


def _extract_budget_limit(session) -> float | None:
    """从会话约束中提取最严格的硬预算上限（无硬预算约束返回 None）。

    D2 主门依据：支付前必须显式 check_budget，预算超限不允许创建 PayPal 订单。
    """
    limit: float | None = None
    for c in getattr(session, "constraints", []) or []:
        if getattr(c, "type", None) != "budget":
            continue
        if not getattr(c, "hard", False):
            continue  # 软预算不做强制拦截
        if getattr(c, "status", "active") != "active":
            continue
        nv = getattr(c, "numeric_value", None)
        if nv is None:
            continue
        limit = float(nv) if limit is None else min(limit, float(nv))
    return limit


def _get_or_create_order(session, user_id: str, total: float, items: list[dict]):
    """复用会话进行中的订单；不存在则创建新的 draft 订单记录（H1 接入）。

    复用范围只覆盖未终结状态（draft / confirmed / paying）：
      - 已取消（cancelled）的订单允许重新建单，否则用户取消后永远无法再次支付
        （session.order_id 会一直指向那条终态订单，后续 create_order 直接被 409 拒绝）。
      - 已支付（paid）的订单仍复用，由调用方返回 409 阻止重复支付（防重复扣款）。
    """
    from app.services.group_purchase.order_lifecycle import (
        create_order_record,
        get_order_record,
        OrderStatus,
    )

    if session.order_id:
        existing = get_order_record(session.order_id)
        if existing is not None and existing.status != OrderStatus.CANCELLED:
            return existing

    plan_id = None
    if getattr(session, "selected_plan", None) is not None:
        plan_id = getattr(session.selected_plan, "plan_id", None)
    order = create_order_record(
        session_id=session.session_id,
        user_id=user_id,
        total=total,
        items=items,
        plan_id=plan_id,
    )

    # 新建订单立即回挂会话，避免重试时反复建单（例如预算守卫拦截后用户反复点击）。
    try:
        session.order_id = order.order_id
        session.order_status = order.status.value
        update_session(session)
    except Exception as exc:  # pragma: no cover - 回挂失败不影响建单
        logger.warning("session order link failed: %s", exc)

    return order


@router.get("/cart/total", tags=["paypal"])
async def get_cart_total(session_id: str = Query(...)):
    """返回当前购物车总价（用于前端展示，不可用于创建订单）。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    cart_items = session.cart_items or []
    total = sum(item.price * item.quantity for item in cart_items)
    
    return {
        "session_id": session_id,
        "items_count": len(cart_items),
        "total": round(total, 2),
        "currency": "USD"
    }


@router.post("/create_order", response_model=CreateOrderResponse)
async def create_order(req: CreateOrderRequest):
    """创建 PayPal 订单并返回 approval_url（用户需在此批准）。

    P0 HITL 完整性修复：只使用 session 购物车计算总价，忽略前端传入的 amount。
    """
    try:
        settings = get_settings()

        # HITL 完整性：金额必须来自 session 购物车
        session = get_session(req.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        cart_items = session.cart_items or []
        if not cart_items:
            raise HTTPException(
                status_code=400,
                detail="购物车为空，无法创建支付订单。请先添加商品。"
            )
        
        # 强制使用购物车总价，忽略前端传入的 amount
        cart_total = sum(item.price * item.quantity for item in cart_items)
        
        # 如果前端传了错误 amount，记录警告便于审计
        if abs(req.amount - cart_total) > 0.01:
            logger.warning(
                "HITL violation: frontend requested %.2f but cart total is %.2f. "
                "Using cart total (%.2f) for order creation. Session: %s",
                req.amount, cart_total, cart_total, req.session_id
            )

        # 记录购物车快照到待处理订单，供审计使用
        cart_snapshot = [
            {
                "id": item.id,
                "name": item.name,
                "price": item.price,
                "quantity": item.quantity,
                "category": item.category,
            }
            for item in cart_items
        ]
        
        # ============ H1 状态机 + D2 预算主门（在调用 PayPal API 之前） ============
        # 复用会话订单，否则创建 draft 订单记录
        from app.services.group_purchase.order_lifecycle import (
            OrderStatus,
            attach_paypal_token,
            begin_payment,
            cancel_order,
            confirm_order,
            refresh_order_snapshot,
        )
        order = _get_or_create_order(
            session, req.user_email or "demo-user", cart_total, cart_snapshot
        )

        # 终态订单禁止再次发起支付（幂等/异常保护）
        if order.status == OrderStatus.PAID:
            raise HTTPException(status_code=409, detail="订单已支付，禁止重复创建支付订单")
        if order.status == OrderStatus.PAYING:
            # 幂等复用：同一会话正在支付中，重复点击“确认支付”不应再造一个 PayPal 订单。
            # 返回上一次的 approval_url（内存登记表里仍保留），前端可继续跳转批准。
            token = order.paypal_token or ""
            pending = _PENDING_ORDERS.get(token, {})
            if token and pending.get("approval_url"):
                return CreateOrderResponse(
                    order_id=token,
                    approval_url=pending["approval_url"],
                    status="CREATED",
                    message="订单已创建，请继续在 PayPal 页面完成批准（未新建 PayPal 订单）",
                    idempotent=True,
                )
            raise HTTPException(status_code=409, detail="支付已在进行中，请先完成当前 PayPal 批准或取消")

        # 订单进入 paying 前，以当前购物车刷新金额与快照
        if order.status in (OrderStatus.DRAFT, OrderStatus.CONFIRMED):
            refresh_order_snapshot(order, total=cart_total, items=cart_snapshot)

        # draft → confirmed：先写 review_confirmed 审计（HITL 信号），再按守卫转移
        # 守卫要求存在匹配该订单的 review_confirmed 事件——没有 review 记录进不了 paying。
        if order.status == OrderStatus.DRAFT:
            try:
                selected_plan_id = None
                if session.selected_plan is not None:
                    selected_plan_id = session.selected_plan.plan_id

                from app.services.group_purchase.audit_log import log_review_confirmation
                log_review_confirmation(
                    session_id=req.session_id,
                    user_id=req.user_email or "demo-user",
                    actor="user",
                    final_items=cart_snapshot,
                    final_total=cart_total,
                    split_preview=[],
                    risks_acknowledged=True,
                    order_id=order.order_id,
                    plan_id=selected_plan_id,
                )
                logger.info(
                    "audit review_confirmed written: order_id=%s plan_id=%s total=%.2f",
                    order.order_id, selected_plan_id, cart_total,
                )
            except Exception as audit_err:
                logger.warning("audit review_confirmed failed: %s", audit_err)

            ok, msg = confirm_order(order, actor="user")
            if not ok:
                raise HTTPException(status_code=409, detail=msg)

        # D2 主门：confirmed → paying 的转移守卫 = check_budget PASS。
        # 预算超限时不创建 PayPal 订单，直接走 J 兜底分支（budget_violated + fallback）。
        budget_limit = _extract_budget_limit(session)
        budget_check = None
        if budget_limit is not None:
            from app.services.group_purchase.budget_guardrail import check_budget
            budget_check = check_budget(cart_snapshot, budget_limit, allow_substitute=True)

            if not budget_check["ok"]:
                from app.services.group_purchase.audit_log import (
                    log_budget_violation,
                    log_fallback_trigger,
                )
                log_budget_violation(
                    session_id=req.session_id,
                    user_id=req.user_email or "demo-user",
                    actor="paypal_callback",
                    budget_limit=budget_limit,
                    actual_total=cart_total,
                    resolution=budget_check["resolution"],
                )
                log_fallback_trigger(
                    session_id=req.session_id,
                    user_id=req.user_email or "demo-user",
                    actor="paypal_callback",
                    reason=f"支付前预算守卫拦截:{cart_total:.2f} > {budget_limit:.2f}",
                    suggested_action=budget_check["resolution"],
                )
                raise HTTPException(
                    status_code=400,
                    detail={
                        "status": "budget_violated",
                        "message": f"🚨 预算守卫拦截支付：{budget_check['resolution']}",
                        "overage": budget_check["overage"],
                        "cart_total": cart_total,
                        "budget_limit": budget_limit,
                        "fallback": budget_check["resolution"],
                    },
                )

        # confirmed → paying：守卫 = check_budget PASS（budget_check=None 表示未设硬预算→放行）
        ok, msg = begin_payment(order, actor="system", budget_check=budget_check)
        if not ok:
            raise HTTPException(status_code=409, detail=msg)

        # ============ 调用 PayPal API（预算已 PASS） ============
        gateway = get_paypal_gateway()
        return_url = _with_session(settings.paypal_return_url, req.session_id)
        cancel_url = _with_session(settings.paypal_cancel_url, req.session_id)
        try:
            result = gateway.create_order(
                cart_total, req.item_name, return_url=return_url, cancel_url=cancel_url
            )
        except Exception as exc:
            # 网关失败必须回滚状态机：否则订单停在 paying 且没有 token，
            # 用户重试只会拿到 409「支付已在进行中」，整条支付链路卡死。
            cancel_order(
                order,
                actor="paypal_callback",
                reason=f"PayPal 创建订单失败:{exc}",
            )
            logger.error(
                "PayPal create_order failed, order %s rolled back to cancelled: %s",
                order.order_id, exc,
            )
            raise HTTPException(status_code=502, detail=f"PayPal 创建订单失败：{exc}")

        # 绑定网关令牌 + 登记待处理订单（带业务订单号，回调侧还原）
        attach_paypal_token(order, result["order_id"])
        _PENDING_ORDERS[result["order_id"]] = {
            "session_id": req.session_id,
            "user_email": req.user_email,
            "item_name": req.item_name,
            "amount": cart_total,
            "cart_snapshot": cart_snapshot,
            "local_order_id": order.order_id,
            "approval_url": result["approval_url"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "PayPal order created: order_id=%s business_order=%s cart_total=%.2f items=%d session_id=%s",
            result["order_id"], order.order_id, cart_total, len(cart_items), req.session_id,
        )

        return CreateOrderResponse(
            order_id=result["order_id"],
            approval_url=result["approval_url"],
            status=result["status"],
            message="订单已创建，请批准"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Create order failed: %s", e)
        raise HTTPException(status_code=500, detail=f"创建订单失败：{e}")


@router.get("/callback")
async def paypal_callback(
    request: Request,
    token: str = Query(..., description="PayPal 返回的支付令牌"),
    PayerID: Optional[str] = Query(None, description="PayPal 买家 ID"),
    session_id: Optional[str] = Query(None, description="ECV 会话 ID（用于审计）")
):
    """PayPal 沙盒回跳回调，捕获订单并推进状态机 paying → paid。

    D2 副门：captured_amount 必须等于订单金额，否则告警并回退 cancelled。
    幂等：订单已是 paid 时直接返回成功，不重复 capture、不重复记 payment_completed。
    """
    ctx = _PENDING_ORDERS.get(token, {})
    session_id = session_id or ctx.get("session_id") or ""
    local_order_id = ctx.get("local_order_id") or ""

    try:
        from app.services.group_purchase.order_lifecycle import (
            OrderStatus,
            cancel_order,
            complete_payment,
            get_order_record,
        )

        order = get_order_record(local_order_id) if local_order_id else None

        # 进程重启（--reload / 崩溃）会清空 _PENDING_ORDERS：用回跳 URL 里的 session_id
        # 反查业务订单兜底。必须校验 paypal_token 一致，否则可能把 A 订单的回调套到 B 订单上。
        if order is None and session_id:
            session = get_session(session_id)
            candidate = getattr(session, "order_id", None) if session else None
            if candidate:
                record = get_order_record(candidate)
                if record is not None and record.paypal_token == token:
                    order = record
                    local_order_id = record.order_id
                    logger.info("callback recovered order %s from session (pending map lost)", candidate)

        if order is None:
            raise HTTPException(status_code=409, detail="未找到对应的业务订单记录（local_order_id 缺失）")

        # 以订单记录为准还原用户身份，避免 _PENDING_ORDERS 丢失后审计写到 demo-user。
        user_id = ctx.get("user_email") or order.user_id or "demo-user"

        # 幂等：已支付订单收到重复回调 → 直接成功返回，不重复 capture / 不重复记审计
        if order.status == OrderStatus.PAID:
            logger.info("idempotent callback: order %s already paid, skip capture", order.order_id)
            if _wants_html(request):
                return RedirectResponse(
                    f"{_frontend_base()}/payment/success?order_id={order.order_id}&session_id={session_id}",
                    status_code=302,
                )
            return {
                "success": True,
                "order_id": order.order_id,
                "paypal_token": token,
                "status": "COMPLETED",
                "amount": order.total,
                "idempotent": True,
                "message": "订单已支付（幂等返回）",
            }

        # 已取消订单的迟到回调：返回取消状态
        if order.status == OrderStatus.CANCELLED:
            logger.info("late callback for cancelled order %s, ignore", order.order_id)
            if _wants_html(request):
                return RedirectResponse(
                    f"{_frontend_base()}/payment/review?session_id={session_id}&status=cancelled",
                    status_code=302,
                )
            return {"success": False, "order_id": order.order_id, "status": "CANCELLED", "message": "订单已取消"}

        # 细节 1：幂等判断必须保持在 callback 入口最前（在任何 capture / 审计写入之前）。
        # 若放在中间，重复回调会走到 payment_completed 已写入之后才被拦截，
        # 审计计数又开始不可信——H1 修好的问题会被顺序调整重新打开。
        # 顺序：幂等拦截 → D2 副门金额校验 → PASS 则 payment_completed → complete_payment
        gateway = get_paypal_gateway()
        order_id = token
        capture_result = gateway.capture_order(order_id)

        if capture_result["status"] == "COMPLETED":
            captured_amount = float(capture_result["amount"])
            logger.info(
                "PayPal payment completed: order_id=%s business_order=%s, amount=%.2f, session_id=%s",
                order_id, order.order_id, captured_amount, session_id
            )

            # D2 副门：paying → paid 守卫 = captured_amount == 订单金额（纵深防御）。
            # 守卫失败 → 告警 + 回退 cancelled（金额 不符时绝不落 paid）。
            # golden path 审计顺序：payment_completed 先落，mark paid 再推进——
            #   先显式跑守卫（不转移状态），通过后写 payment_completed，
            #   最后 complete_payment 内部再跑一次守卫并翻状态（幂等，双保险）。
            from app.services.group_purchase.order_lifecycle import guard_amount_matched

            guard_ok, guard_msg = guard_amount_matched(captured_amount, order.total)
            if not guard_ok:
                logger.warning("AMOUNT_MISMATCH GUARD: %s (order=%s token=%s)", guard_msg, order.order_id, token)
                # 细节 2：D2 副门失败 != budget_violated。写独立的 payment_amount_mismatch 告警审计，
                # 带 captured_amount / expected_amount / paypal_token，保证金额篡改在审计里可见；
                # 否则静默取消就会形成审计盲区。随后 cancel_order 会再写 order_status_changed(paying→cancelled)。
                from app.services.group_purchase.audit_log import log_payment_amount_mismatch
                log_payment_amount_mismatch(
                    session_id=session_id,
                    user_id=user_id,
                    actor="paypal_callback",
                    order_id=order.order_id,
                    captured_amount=captured_amount,
                    expected_amount=order.total,
                    paypal_token=token,
                )
                cancel_order(
                    order,
                    actor="paypal_callback",
                    reason=f"金额不符告警:{captured_amount:.2f} != {order.total:.2f}",
                )
                if _wants_html(request):
                    return RedirectResponse(
                        f"{_frontend_base()}/payment/review"
                        f"?session_id={session_id}&status=cancelled&reason=amount_mismatch",
                        status_code=302,
                    )
                return {
                    "success": False,
                    "order_id": order.order_id,
                    "status": "CANCELLED",
                    "reason": "amount_mismatch",
                    "message": f"金额校验失败：{guard_msg}",
                }

            # 守卫通过 → 先落 payment_completed（“支付完成”这个事实），
            # 再推进状态机 paying → paid —— 时间线顺序与 golden path 剧本一致：
            #   payment_completed → order_status_changed(paying→paid)
            #
            # 细节 3：payment_completed 写完 → complete_payment 执行之间存在崩溃窗口。
            # 若在此窗口内进程崩溃，会出现"审计事实已落盘（payment_completed）但订单状态仍 paying"的悬挂。
            # audit_log 是 append-only，不能回滚已写事件。demo 项目先接受该窗口：
            # 在 order_lifecycle.py 启动时做一次对账（reconcile_sessions_on_startup：
            # 扫描 payment_completed 有但订单仍 paying 的，触发补偿转移到 paid），
            # 未来生产化时再换成分布式事务/两阶段提交。
            # 绝不把 payment_completed 塞回 complete_payment 内部靠时间戳维持语义——那等于绕回原点。
            _log_payment_completed(
                order_id=order.order_id,
                session_id=session_id,
                user_id=user_id,
                amount=captured_amount,
            )

            ok, msg = complete_payment(order, captured_amount, expected_total=order.total)
            if not ok:
                # 理论不可达：上一步守卫刚通过。防御性记录，不改变状态机结果。
                logger.warning("complete_payment 二次守卫异常（应已在上面拦截）: %s", msg)

            success_url = (
                f"{_frontend_base()}/payment/success"
                f"?order_id={order.order_id}"
                f"&session_id={session_id}"
                f"&amount={captured_amount:.2f}"
                f"&paypal_token={order_id}"
            )

            if _wants_html(request):
                return RedirectResponse(success_url, status_code=302)

            return {
                "success": True,
                "order_id": order.order_id,
                "paypal_token": order_id,
                "status": "COMPLETED",
                "amount": captured_amount,
                "seller_protection": capture_result["seller_protection"],
                "redirect_url": success_url,
                "message": "支付成功，订单已创建"
            }
        else:
            raise HTTPException(status_code=400, detail="支付未完成")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("PayPal callback failed: %s", e)
        raise HTTPException(status_code=500, detail=f"支付处理失败：{e}")


@router.get("/cancel")
async def paypal_cancel(
    request: Request,
    session_id: Optional[str] = Query(None),
    token: Optional[str] = Query(None, description="PayPal 取消时带回的订单令牌")
):
    """用户在 PayPal 页面点击 Cancel，返回前端。状态机：paying → cancelled。"""
    ctx = _PENDING_ORDERS.get(token or "", {})
    session_id = session_id or ctx.get("session_id") or ""
    local_order_id = ctx.get("local_order_id") or ""

    # H1：推进状态机 paying → cancelled（幂等：已 cancelled 则跳过）
    try:
        from app.services.group_purchase.order_lifecycle import (
            OrderStatus,
            cancel_order,
            get_order_record,
        )
        order = get_order_record(local_order_id) if local_order_id else None
        # _PENDING_ORDERS 在进程重启后丢失时，按 session_id 兜底找回订单，
        # 否则用户取消后订单会一直挂在 paying，订单档案页也会被 4s 轮询拖住。
        if order is None and session_id:
            session = get_session(session_id)
            candidate = getattr(session, "order_id", None) if session else None
            order = get_order_record(candidate) if candidate else None
        if order is not None and order.status in (OrderStatus.DRAFT, OrderStatus.CONFIRMED, OrderStatus.PAYING):
            ok, msg = cancel_order(order, actor="paypal_callback", reason="用户在 PayPal 页面取消")
            logger.info("PayPal cancel state machine: order=%s ok=%s msg=%s", order.order_id, ok, msg)
    except Exception as exc:  # pragma: no cover - 取消不应因状态机异常而失败
        logger.warning("paypal_cancel state machine failed: %s", exc)

    cancel_url = (
        f"{_frontend_base()}/payment/review"
        f"?session_id={session_id}&status=cancelled&token={token or ''}"
    )

    logger.info("PayPal payment cancelled: token=%s session_id=%s", token, session_id)

    if _wants_html(request):
        return RedirectResponse(cancel_url, status_code=302)

    return {
        "success": False,
        "status": "CANCELLED",
        "message": "用户取消支付",
        "redirect_url": cancel_url
    }