"""支付 Agent API —— 授权边界主图的 HTTP 入口。

RootUser 裁决的 P0-1：把含支付子图的 build_agent_graph() 主图接进 HTTP。
购物意图优先进入支付子图；之前 /chat/agent 只接了文化推荐图，支付子图只能被
demo.py / _e2e_test.py / 测试调用。这里补上真正的入口：

  POST /api/v1/payment/agent   第一程：用户消息 -> 主图（HITL 中断时停在审批闸门）
  POST /api/v1/payment/approve 第二程：Command(update=...) 恢复中断，继续跑完支付

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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/payment", tags=["payment"])

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

                _graph_instance = build_agent_graph()
                logger.info("payment graph built (singleton, interrupt_before=payment_approval_gate)")
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
        from app.services.paypal.authz import TrustProfile

        return TrustProfile(
            trust_score=float(value.get("trust_score", 0.3)),
            accurate_actions=int(value.get("accurate_actions", 0)),
            failed_actions=int(value.get("failed_actions", 0)),
            auto_threshold=float(value.get("auto_threshold", 20.0)),
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
        reply = state.get("paypal_msg", "") or "处理完成。"

    return PaymentResponse(
        reply=reply,
        needs_approval=needs_approval,
        status=approval_status or paypal_status or verdict,
        payment={
            "item": item,
            "rank_source": panel.get("rank_source", ""),
            "mode": payment.get("mode", ""),
            "order_id": payment.get("order_id", ""),
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


# ===== 端点 =====


@router.post("/agent", response_model=PaymentResponse)
async def payment_agent(payload: PaymentAgentRequest):
    """第一程：用户消息 -> 主图。

    支付意图经 route_after_llm 路由进支付子图；需要人工确认时停在
    payment_approval_gate（interrupt_before），返回 needs_approval=true。
    """
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

    # P0-3：会话结束，把图演化出的最终信任画像（A/B 级证据累计）写回长期记忆，
    # 让下一次会话（同一 user_id）从这份画像开始，而不是重回 0.3。
    final_trust = result.get("trust_profile")
    if final_trust is not None:
        _save_trust_profile(payload.user_id, final_trust)

    return _state_to_response(result, payload.session_id, result.get("trace_id", ""))


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