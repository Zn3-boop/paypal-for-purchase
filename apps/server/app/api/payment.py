"""支付 Agent API —— 授权边界主图的 HTTP 入口。

RootUser 裁决的 P0-1：把含支付子图的 build_agent_graph() 主图接进 HTTP。
购物意图优先进入支付子图；之前 /chat/agent 只接了文化推荐图，支付子图只能被
demo.py / _e2e_test.py / 测试调用。这里补上真正的入口：

  POST /api/v1/payment/agent   第一程：用户消息 -> 主图（HITL 中断时停在审批闸门）
  POST /api/v1/payment/approve 第二程：Command(update=...) 恢复中断，创建 PayPal 订单
  GET  /api/v1/paypal/callback  第三程：买家回跳后恢复图，捕获订单（与团购共用入口）

关键工程约束：
  - 跨请求恢复中断依赖**同一个 compiled graph + 同一个 checkpointer**（thread_id
    锁定会话）。所以这里用模块级单例缓存图实例，而不是每次请求 build 新图——
    否则 MemorySaver 里的 checkpoint 随请求结束而丢失，"批准"永远找不到"待审批"。
  - 响应体只挑 JSON-safe 字段：state 里的 TrustProfile/GoalMemory 是 dataclass
    实例，不能直接序列化；统一走 assemble_explainability_panel（已剥离非 JSON 类型）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/payment", tags=["payment"])

# Demo-scoped in-memory continuation. Production must persist this across workers.
_PENDING_PAYMENTS: dict[str, dict[str, str]] = {}

logger = logging.getLogger(__name__)

# ===== 单例 compiled graph（checkpoint 跨请求存活的关键） =====
_graph_instance: Any = None
_graph_lock = threading.Lock()


def _get_graph():
    """返回进程级单例主图（含支付子图 + interrupt_before）。

    build_agent_graph() 默认 checkpointer=_UNSET -> get_checkpointer() 返回
    MemorySaver（GRAPH_ENV=dev）。缓存同一实例，审批恢复才能命中同一个 checkpoint。
    """
    global _graph_instance
    if _graph_instance is None:
        with _graph_lock:
            if _graph_instance is None:
                from app.services.graph.graph import build_agent_graph

                from app.services.payment_agent.nodes import PAYMENT_MODE

                _graph_instance = build_agent_graph(
                    interrupt_before_paypal_capture=PAYMENT_MODE == "real"
                )
                logger.info(
                    "payment graph built (singleton; mode=%s, buyer_return_gate=%s)",
                    PAYMENT_MODE,
                    PAYMENT_MODE == "real",
                )
    return _graph_instance


# ===== 信任画像跨会话持久化（P0-3：信任分跨会话恢复） =====
# 图内 TrustProfile 是会话级 dataclass；这里用 graph.memory_store（长期记忆，
# 按 user 隔离 + filelock 并发安全）落地"用户 -> 最新信任画像"，让新会话
# authority_check 一开始就带上历史 A/B 级证据演化出的分数，而不是重回 0.3。

TRUST_MEMORY_KEY = "payment_trust_profile"
TRUST_MEMORY_OWNER = "payment_agent/authz"


def _load_trust_profile(user_id: str):
    """从长期记忆读用户上次会话结束时的信任画像；无记录/异常 -> None（图内默认）。"""
    if not user_id:
        return None
    try:
        from app.services.graph.memory_store import get_memory_store

        entry = get_memory_store().get(user_id, TRUST_MEMORY_KEY)
        value = (entry or {}).get("value") or {}
        if not value:
            return None
        from app.services.paypal.authz import AUTO_THRESHOLD_DEFAULT, TrustProfile

        return TrustProfile(
            trust_score=float(value.get("trust_score", 0.3)),
            accurate_actions=int(value.get("accurate_actions", 0)),
            failed_actions=int(value.get("failed_actions", 0)),
            auto_threshold=float(value.get("auto_threshold", AUTO_THRESHOLD_DEFAULT)),
        )
    except Exception as exc:  # pragma: no cover - 加载失败不硬失败，回退默认画像
        logger.warning("load_trust_profile failed for user=%s: %s", user_id, exc)
        return None


def _save_trust_profile(user_id: str, trust_profile) -> None:
    """把最新信任画像写回长期记忆（A 级证据资产，带 owner 实体防腐烂）。"""
    if not user_id or trust_profile is None:
        return
    try:
        from app.services.graph.memory_store import get_memory_store

        get_memory_store().put(
            user_id,
            TRUST_MEMORY_KEY,
            {
                "trust_score": float(trust_profile.trust_score),
                "accurate_actions": int(trust_profile.accurate_actions),
                "failed_actions": int(trust_profile.failed_actions),
                "auto_threshold": float(trust_profile.auto_threshold),
            },
            level="A",
            owner=TRUST_MEMORY_OWNER,
        )
        logger.info(
            "trust profile persisted: user=%s score=%.3f acc=%d fail=%d",
            user_id,
            trust_profile.trust_score,
            trust_profile.accurate_actions,
            trust_profile.failed_actions,
        )
    except Exception as exc:  # pragma: no cover - 持久化失败不阻断支付
        logger.warning("save_trust_profile failed for user=%s: %s", user_id, exc)


# ===== 请求 / 响应模型 =====


class PaymentAgentRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户输入，例如：帮我买一张周五晚上听的爵士唱片")
    session_id: str = Field(default="default", description="会话 key（线程 ID 的一部分，跨请求恢复用）")
    user_id: str = Field(default="demo-user", description="用户标识（信任画像/session 归属）")
    monthly_spent: float = Field(default=0.0, description="本月已消费金额（预算/冲动检查用）")
    purchase_history: list[dict[str, Any]] = Field(default_factory=list, description="近期购买历史（行为信号用）")


class PaymentApproveRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="同待审批轮次的 session_id")
    user_id: str = Field(default="demo-user", description="同待审批轮次的 user_id")
    decision: Literal["approve", "reject", "modify"] = Field(..., description="审批决策")
    comment: str = Field(default="", max_length=500, description="审批备注（写入 approval_comment / 证据回写）")


class PaymentGoalRequest(BaseModel):
    user_id: str = Field(default="demo-user", description="目标归属的用户标识")
    session_id: str = Field(default="default", description="与支付会话同 key（目标记忆按 session 收敛，authority_check 能读到）")
    text: str = Field(..., min_length=2, max_length=200, description="储蓄目标话术，如：我要存钱买相机")
    monthly_spend_cap: float = Field(default=300.0, description="月度消费上限（防御性护栏，覆盖默认值）")


class PaymentGoalResponse(BaseModel):
    ok: bool = Field(..., description="是否成功解析并存下目标")
    goal: dict[str, Any] | None = Field(default=None, description="本次落库的目标（结构化约束）")
    active_goals: list[dict[str, Any]] = Field(default_factory=list, description="该用户所有激活目标")
    message: str = Field(default="", description="给前端展示的中文结果")


class PaymentResponse(BaseModel):
    reply: str = Field(..., description="给用户看的中文回复")
    needs_approval: bool = Field(default=False, description="是否停在审批闸门（等待前端批准/否决）")
    status: str = Field(default="", description="approval_status / 支付状态")
    payment: dict[str, Any] = Field(default_factory=dict, description="商品 + PayPal 订单信息")
    authority: dict[str, Any] = Field(default_factory=dict, description="授权判定（verdict/reason/trust_score/...）")
    approval: dict[str, Any] = Field(default_factory=dict, description="审批状态/备注/证据回写")
    explain: dict[str, Any] = Field(default_factory=dict, description="事后解释 + 信任分回写")
    evidence: list[dict[str, Any]] = Field(default_factory=list, description="可解释性面板证据分组")
    llm_source: str = Field(default="", description="本轮意图/理由真实来源（llm / rule / fallback）")
    session_id: str = Field(default="", description="回显 session_id（前端用它做后续审批）")
    trace_id: str = Field(default="", description="回显 trace_id")
# ===== 内部工具 =====


def _new_state(
    raw_query: str,
    session_id: str,
    user_id: str,
    monthly_spent: float,
    purchase_history: list[dict[str, Any]],
) -> dict[str, Any]:
    """init_state 完整初始化 + 注入支付场景字段（对齐测试里 _base_state 的形态）。"""
    from app.services.graph.state import init_state

    state = init_state(
        raw_query=raw_query,
        session_id=session_id,
        user_id=user_id,
    )
    state["monthly_spent"] = monthly_spent or 0.0
    state["purchase_history"] = purchase_history or []
    state["goal_memory"] = None  # authority_check 内按 session_id 自取（跨会话目标记忆）
    # P0-3：从长期记忆装载历史信任画像（无记录 -> None，authority_check 内默认 TrustProfile()）
    state["trust_profile"] = _load_trust_profile(user_id)
    return state


def _format_catalog_hint(candidates: list[dict[str, Any]]) -> str:
    """目录外商品兜底：把目录里真实存在的候选列出来，避免"处理完成。"式空回复。"""
    rows: list[str] = []
    for item in candidates[:4]:
        name = item.get("name", "")
        if not name:
            continue
        price = item.get("price")
        if price is None:
            rows.append(name)
            continue
        rows.append(f"{name}（{price} {item.get('currency', 'USD')}）")
    if not rows:
        return ""
    return "当前小铺里可选的有：" + "、".join(rows) + "。"


def _state_to_response(
    state: dict[str, Any], session_id: str, trace_id: str
) -> PaymentResponse:
    """把图产出的 AgentState 转成 JS-safe 响应（剥离 dataclass，走 panel 数据源）。"""
    from app.services.payment_agent.panel import (
        assemble_explainability_panel,
        evidence_groups_for_panel,
    )

    panel = assemble_explainability_panel(state)  # 已做 JSON-safe 剥离
    evidence = evidence_groups_for_panel(state)

    authority = panel.get("authority", {}) or {}
    approval = panel.get("approval", {}) or {}
    payment = panel.get("payment", {}) or {}
    explain = state.get("explanation", {}) or {}
    trust_updated = state.get("trust_updated", {}) or {}

    verdict = authority.get("verdict", "")
    approval_status = state.get("approval_status", "") or ""
    paypal_status = state.get("paypal_status", "") or ""

    needs_approval = verdict == "need_approval" and approval_status in ("", "pending")

    # ---- 组装用户可见回复 ----
    item = panel.get("item", {}) or {}
    item_name = item.get("name", "")
    item_price = item.get("price")
    currency = item.get("currency", "USD")

    if needs_approval:
        reply = (
            f"已为你找到「{item_name or '候选商品'}」"
            f"（{item_price} {currency}）。"
            f"这笔支付需要你确认后才能完成。"
        )
        if authority.get("reason"):
            reply += f" 理由：{authority['reason']}"
    elif verdict == "block":
        reply = authority.get("reason") or "这笔支付被授权边界拦截了。"
    elif paypal_status == "created" and payment.get("approval_url"):
        reply = "已创建 PayPal 订单，正在跳转到 PayPal。请完成买家审批；返回后才会捕获扣款。"
    elif paypal_status == "captured" or approval_status == "approved":
        reply = (
            state.get("paypal_msg", "")
            or f"支付完成：{item_name or ''}（{item_price} {currency}）已扣款。"
        )
        if explain.get("text"):
            reply = explain["text"]
    elif approval_status == "cancelled":
        reply = f"已按你的决定取消这笔支付（{item_name or ''}）。"
    else:
        # 空兜底不再返回"处理完成。"（用户事故：说"帮我找副降噪耳机"只看到这四个字）。
        # 两种诚实收口：
        #   1) 进了支付子图但目录里没有相关商品 -> 列出目录真实可选 + 指路拼单
        #   2) 根本没进支付子图（非购物请求）-> 说明没产生购买动作 + 给出可用说法
        shopping_error = state.get("shopping_error", "") or ""
        if shopping_error == "no_relevant_candidates":
            raw = (state.get("raw_query", "") or "").strip()
            hint = _format_catalog_hint(state.get("shopping_candidates", []) or [])
            reply = (
                f"在商品目录里没有找到「{raw or '这类商品'}」。{hint}"
                "如果要给社团/多人批量采购（饮用水 / 装饰 / 奖品 / 文具），"
                "请到「📊 方案对比」：约束 → 生成方案 → 确认下单 → 一键跳转 PayPal。"
            )
        else:
            reply = state.get("paypal_msg", "") or (
                "这条消息没有产生购买动作。"
                "买一件东西可以说「帮我买…」或「帮我找…（带预算）」，"
                "多人拼单请到「📊 方案对比」。"
            )

    return PaymentResponse(
        reply=reply,
        needs_approval=needs_approval,
        status=approval_status or paypal_status or verdict,
        payment={
            "item": item,
            "rank_source": panel.get("rank_source", ""),
            "mode": payment.get("mode", ""),
            "order_id": payment.get("order_id", ""),
            "paypal_order_id": state.get("paypal_order_id", ""),
            "business_order_id": state.get("business_order_id", ""),
            "capture_id": payment.get("capture_id", ""),
            "status": payment.get("status", ""),
            "approval_url": payment.get("approval_url", ""),
            "msg": payment.get("msg", ""),
            "error": payment.get("error", ""),
            "source": payment.get("source", ""),
            "intent_id": payment.get("intent_id", ""),
            "evidence_caveats": payment.get("evidence_caveats", []),
        },
        authority=authority,
        approval={
            "status": approval.get("status", ""),
            "comment": approval.get("comment", ""),
            "evidence_applied": approval.get("evidence_applied", {}),
        },
        explain={
            "text": explain.get("text", ""),
            "llm_source": explain.get("llm_source", ""),
            "structured": explain.get("structured", {}),
            "trust_updated": trust_updated,
        },
        evidence=evidence,
        llm_source=state.get("llm_source", "") or authority.get("llm_source", ""),
        session_id=session_id,
        trace_id=trace_id or state.get("trace_id", ""),
    )


def _register_pending_payment(
    result: dict[str, Any], *, session_id: str, user_id: str
) -> None:
    """应用授权通过后只登记 PayPal 建单事实；回跳捕获依赖此上下文。"""
    if result.get("paypal_status") == "created" and result.get("paypal_order_id"):
        _PENDING_PAYMENTS[session_id] = {
            "user_id": user_id,
            "token": result["paypal_order_id"],
        }


# ===== 支付 Agent → 审计时间轴 + 订单档案（与团购共用） =====
# 用户抱怨「审计时间轴和订单档案连'创建失败'都不显示」的修复：
# 支付 Agent 每笔真实的 PayPal 建单/捕获都会在这里落两条留痕——
#   a) audit_log 的 payment_started / payment_failed 事件（时间轴可见）；
#   b) order_lifecycle 的 OrderRecord（订单档案可见，含 status=paying / paid / cancelled）。
# 去重策略：以 (session, paypal_order_id) 为幂等键，重复调用不会写两次。

_PAYMENT_EVENT_DONE: set[tuple[str, str, str]] = set()  # (session_id, order_key, status)
_AGENT_ORDERS: dict[tuple[str, str], str] = {}  # (session_id, paypal_order_id) -> business order_id
_AGENT_ORDERS_LOCK = threading.Lock()


def _agent_order_for(session_id: str, paypal_order_id: str) -> str:
    with _AGENT_ORDERS_LOCK:
        return _AGENT_ORDERS.get((session_id, paypal_order_id), "")


def _set_agent_order(session_id: str, paypal_order_id: str, order_id: str) -> None:
    with _AGENT_ORDERS_LOCK:
        _AGENT_ORDERS[(session_id, paypal_order_id)] = order_id


def _record_agent_activity(
    state: dict[str, Any], *, session_id: str, user_id: str
) -> None:
    """建单 / 捕获 / 失败全部留痕到审计时间轴与订单档案（失败也显示）。"""
    if not session_id:
        return
    from app.services.group_purchase.audit_log import (
        log_payment_event,
        log_review_confirmation,
    )
    from app.services.group_purchase.order_lifecycle import (
        OrderStatus,
        attach_paypal_token,
        begin_payment,
        cancel_order,
        complete_payment,
        confirm_order,
        create_order_record,
        get_order_record,
    )

    selected = state.get("shopping_selected") or {}
    item_name = selected.get("name") or ""
    amount = float(selected.get("price", 0) or 0)
    paypal_status = (state.get("paypal_status") or "").strip()
    paypal_order = (state.get("paypal_order_id") or "").strip()
    paypal_mode = state.get("paypal_mode") or ""
    paypal_source = state.get("paypal_source") or ""
    paypal_msg = state.get("paypal_msg") or ""
    uid = user_id or state.get("user_id") or "demo-user"
    currency = selected.get("currency") or "USD"

    if paypal_status in ("", "none", "pending"):
        return  # 还没走到建单，无事可记

    items = [{
        "id": selected.get("id", ""),
        "name": selected.get("name", ""),
        "price": round(float(selected.get("price") or 0), 2),
        "quantity": 1,
        "subtotal": round(float(selected.get("price") or 0), 2),
        "category": selected.get("category", ""),
        "image_url": selected.get("image_url", ""),
        "source": selected.get("rank_source") or paypal_source or "",
    }]

    order_key = paypal_order or "no-order"
    business_order_id = _agent_order_for(session_id, order_key)

    # ── 1) 先落订单档案，拿到业务订单号（审计事件要与档案里的订单号对得上） ──
    order = None
    try:
        if business_order_id:
            order = get_order_record(business_order_id)
        if order is None:
            order = create_order_record(
                session_id=session_id,
                user_id=uid,
                total=amount,
                currency=currency,
                items=items,
                plan_id="agent",
            )
            business_order_id = order.order_id
            _set_agent_order(session_id, order_key, business_order_id)
            # 支付 Agent 的裁决即「人工复核」：写 review_confirmed 以通过状态机守卫，
            # 让档案能走到 paid / cancelled，同时保留完整审计链。
            log_review_confirmation(
                session_id=session_id,
                user_id=uid,
                actor="payment_agent",
                final_items=items,
                final_total=amount,
                order_id=order.order_id,
                plan_id="agent",
            )
            for ok, msg in (
                confirm_order(order, actor="payment_agent"),
                begin_payment(order, actor="payment_agent"),
            ):
                if not ok:
                    logger.warning("agent order transition skipped: %s", msg)
            if paypal_order:
                attach_paypal_token(order, paypal_order)
        elif paypal_order and not getattr(order, "paypal_token", ""):
            attach_paypal_token(order, paypal_order)
    except Exception as exc:  # 档案写入失败不阻断支付主链路
        logger.warning("agent order archive sync failed: %s", exc)

    # ── 2) 审计事件 payment_started / payment_failed（带业务订单号，时间轴可溯源） ──
    if paypal_status in ("created", "captured", "paying"):
        event = "payment_started"
        reason = "PayPal 订单已创建" + ("（mock）" if paypal_mode == "mock" else "")
    else:
        event = "payment_failed"
        reason = paypal_msg or f"支付未完成：{paypal_status}"
    audit_key = (session_id, order_key, event)
    if audit_key not in _PAYMENT_EVENT_DONE:
        try:
            log_payment_event(
                session_id=session_id,
                user_id=uid,
                actor="payment_agent",
                event=event,
                order_id=business_order_id,
                paypal_order_id=paypal_order,
                amount=amount,
                currency=currency,
                item_name=item_name,
                mode=paypal_mode,
                source=paypal_source,
                reason=reason,
                business_order_id=business_order_id,
            )
            _PAYMENT_EVENT_DONE.add(audit_key)
        except Exception as exc:  # 审计失败不阻断支付主链路
            logger.warning("agent payment audit failed: %s", exc)

    # ── 3) 终态：captured → paid；捕获失败/取消 → cancelled（失败也要在档案留痕） ──
    try:
        if order is not None:
            if paypal_status == "captured":
                if order.status != OrderStatus.PAID:
                    ok, msg = complete_payment(
                        order, captured_amount=amount, actor="payment_agent"
                    )
                    if not ok:
                        logger.warning("agent order paid transition skipped: %s", msg)
            elif paypal_status in ("capture_failed", "failed", "cancelled"):
                if order.status not in (OrderStatus.PAID, OrderStatus.CANCELLED):
                    ok, msg = cancel_order(
                        order,
                        actor="payment_agent",
                        reason=paypal_msg or paypal_status,
                    )
                    if not ok:
                        logger.warning("agent order cancel skipped: %s", msg)
    except Exception as exc:  # 档案写入失败不阻断支付主链路
        logger.warning("agent order finalize failed: %s", exc)

    # 把业务订单号带回响应，前端订单档案可深链展开
    if business_order_id:
        state["business_order_id"] = business_order_id


# ===== 端点 =====


@router.post("/agent", response_model=PaymentResponse)
async def payment_agent(payload: PaymentAgentRequest, request: Request):
    """第一程：用户消息 -> 主图。

    支付意图经 route_after_llm 路由进支付子图；需要人工确认时停在
    payment_approval_gate（interrupt_before），返回 needs_approval=true。
    """
    # 记住浏览器真实 origin（含端口）：PayPal 登录后回跳要用它落地，
    # 否则 .env 的 FRONTEND_BASE_URL 与实际 dev 端口不一致时会跳进白页。
    try:
        from app.api.paypal_callback import remember_frontend_origin

        remember_frontend_origin(payload.session_id, request)
    except Exception:  # pragma: no cover - 记录失败不影响主流程
        pass
    from app.services.graph.graph import build_invoke_config

    graph = _get_graph()
    state = _new_state(
        raw_query=payload.message,
        session_id=payload.session_id,
        user_id=payload.user_id,
        monthly_spent=payload.monthly_spent,
        purchase_history=payload.purchase_history,
    )
    cfg = build_invoke_config(
        user_id=payload.user_id,
        session_id=payload.session_id,
        trace_id=state.get("trace_id", ""),
    )
    try:
        result = await graph.ainvoke(state, cfg)
    except Exception as exc:
        logger.exception("payment/agent graph execution failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Payment agent execution failed: {exc}") from exc

    _register_pending_payment(
        result, session_id=payload.session_id, user_id=payload.user_id
    )
    _record_agent_activity(
        result, session_id=payload.session_id, user_id=payload.user_id
    )
    return _state_to_response(result, payload.session_id, state.get("trace_id", ""))


@router.post("/approve", response_model=PaymentResponse)
async def payment_approve(payload: PaymentApproveRequest):
    """第二程：前端批准/否决 -> Command(update=...) 恢复中断的审批闸门。

    同一个 graph 单例 + 同一个 thread_id（user_id_session_id）命中 checkpoint，
    approval_gate_node 读到 approval_decision 后继续跑支付链路。
    """
    from app.services.graph.graph import build_invoke_config
    from langgraph.types import Command

    graph = _get_graph()
    cfg = build_invoke_config(
        user_id=payload.user_id,
        session_id=payload.session_id,
    )
    try:
        result = await graph.ainvoke(
            Command(
                update={
                    "approval_decision": payload.decision,
                    "approval_comment": payload.comment,
                }
            ),
            cfg,
        )
    except Exception as exc:
        logger.exception("payment/approve resume failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Approval resume failed: {exc}") from exc

    _register_pending_payment(
        result, session_id=payload.session_id, user_id=payload.user_id
    )
    _record_agent_activity(
        result, session_id=payload.session_id, user_id=payload.user_id
    )

    # P0-3：会话结束，把图演化出的最终信任画像（A/B 级证据累计）写回长期记忆，
    # 让下一次会话（同一 user_id）从这份画像开始，而不是重回 0.3。
    final_trust = result.get("trust_profile")
    if final_trust is not None:
        _save_trust_profile(payload.user_id, final_trust)

    return _state_to_response(result, payload.session_id, result.get("trace_id", ""))


@router.get("/callback", response_model=PaymentResponse)
async def payment_callback(session_id: str, token: str, PayerID: str = ""):
    """兼容旧 return_url；新订单统一从 /paypal/callback 分派到这里。"""
    return await complete_payer_return(session_id=session_id, token=token)


async def complete_payer_return(*, session_id: str, token: str) -> PaymentResponse:
    """PayPal 买家回跳后恢复同一图；此入口之前不得执行 capture。"""
    pending = _PENDING_PAYMENTS.get(session_id)
    if not pending or pending["token"] != token:
        raise HTTPException(status_code=409, detail="PayPal 回跳与待支付订单不匹配")
    from app.services.graph.graph import build_invoke_config
    from langgraph.types import Command

    result = await _get_graph().ainvoke(
        Command(update={"payer_approved": True}),
        build_invoke_config(user_id=pending["user_id"], session_id=session_id),
    )
    if result.get("paypal_order_id") != token:
        raise HTTPException(status_code=409, detail="PayPal 回跳订单与图状态不匹配")
    _record_agent_activity(result, session_id=session_id, user_id=pending["user_id"])
    response = _state_to_response(result, session_id, result.get("trace_id", ""))
    if response.payment.get("status") == "captured":
        _PENDING_PAYMENTS.pop(session_id, None)
    if result.get("trust_profile") is not None:
        _save_trust_profile(pending["user_id"], result["trust_profile"])
    return response


@router.post("/goal", response_model=PaymentGoalResponse)
async def payment_goal(payload: PaymentGoalRequest):
    """设置储蓄目标（P1：演示冲突拦截入口）。

    目标落进 GoalMemory（按 session 收敛的进程内 registry，与 authority_check
    读取的是同一实例）——之后任何支付请求命中该目标约束都会进入 blocked 规则，
    需要人工审批，而不是自动放行。适合 3 分钟演示「我要存钱买相机 → 买唱片被拦」。
    """
    from app.services.payment_agent.goals import get_goal_memory

    mem = get_goal_memory(payload.session_id)
    goal = mem.set_goal_from_text(payload.user_id, payload.text)
    # 覆盖月度护栏（默认 300，演示可收紧以观察月度超限拦截）
    goal.monthly_spend_cap = payload.monthly_spend_cap
    active = [
        {
            "target_label": g.target_label,
            "target_amount": g.target_amount,
            "monthly_spend_cap": g.monthly_spend_cap,
            "since": g.since,
            "active": g.active,
        }
        for g in mem.get_goals(payload.user_id)
    ]
    return PaymentGoalResponse(
        ok=True,
        goal={
            "target_label": goal.target_label,
            "target_amount": goal.target_amount,
            "monthly_spend_cap": goal.monthly_spend_cap,
            "since": goal.since,
            "active": goal.active,
        },
        active_goals=active,
        message=(
            f"目标已保存：{goal.target_label}"
            + (f"（储蓄 {goal.target_amount:.2f}）" if goal.target_amount > 0 else "")
            + "。之后买享受型物品会被目标约束拦截，需要你确认。"
        ),
    )