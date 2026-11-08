"""Plan Generation APIs with Budget Guardrail Integration."""
from __future__ import annotations
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.group_purchase.planner import generate_dual_plans
from app.services.group_purchase.session_state import get_session, update_session
from app.services.group_purchase.budget_guardrail import check_budget
from app.services.group_purchase.audit_log import (
    log_plan_generation,
    log_budget_violation,
    log_constraint_change,
    log_intent_submitted,
)
from app.services.shopper.catalog_k1 import get_k1_items

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plans", tags=["plans"])

# 团购/单人模式关键词：同一个提问框，按自然语言判断走哪种采购模式。
# 两者共用水管线（约束 → Channel3 检索 → 双方案 → PayPal），只是表达口径不同。
GROUP_MODE_WORDS = ("人份", "大家", "团", "一起", "拼", "社团", "小组", "每个人", "人手")


class IntentRequest(BaseModel):
    """统一入口：约束看板的输入框与全局 Agent 提问框都走这里。"""

    message: str
    source: str = "agent_composer"


def detect_intent_mode(text: str) -> str:
    """规则判定采购模式：涉及多人/团购词 -> group，否则 single。"""
    tl = (text or "").lower()
    if any(kw in tl for kw in GROUP_MODE_WORDS):
        return "group"
    return "single"


@router.post("/intent")
async def submit_intent(session_id: str = Query(...), req: IntentRequest | None = None):
    """统一入口：把一句需求变成「检索关键词 + 约束 + 新模式」，并让方案重新生成。

    - 单人购买与团购共用这一个入口（同一个 Agent 提问框），不存在两套入口。
    - intent_query 就是随后传给 Channel3 的检索关键词（逐字使用，不二次改写）；
    - 与 GET /plans 的快照语义正交：只有新的 intent（新的检索任务）才会让快照失效，
      约束在旧任务里收紧不会静默改写方案（守卫在支付时刻把关）。
    """
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    text = (req.message if req else "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")

    # 1) 记录检索关键词（Channel3 查询逐字使用）
    session.intent_query = text
    session.intent_mode = detect_intent_mode(text)

    # 2) 即时提取约束（复用约束抽取，LLM 优先 / 规则降级）
    from app.api.constraints import _extract_with_llm

    extracted = await _extract_with_llm(text, req.source if req else "agent_composer")

    # 新提问 = 新采购任务：上一次「由提问自动抽取」的约束必须先退场。
    # 语义答案（用户问的"到底是一次性吗"）：
    #   - 约束是**会话级**的，不是一次性开关：它随会话持续生效；
    #   - 但**新提问会替换上一次提问抽出来的那批**（旧任务的 $350 不该硬卡新任务），
    #     而你在约束看板里手动添加的约束不在 auto_constraint_ids 里，会被保留；
    #   - 所以一般提问时把预算说出来就够了，它会在本任务内持续约束支付时刻。
    replaced_ids: list[str] = []
    for old in list(session.constraints):
        if old.status != "active":
            continue
        if old.id in (session.auto_constraint_ids or []):
            old.status = "removed"
            replaced_ids.append(old.id)
            try:
                log_constraint_change(
                    session_id=session_id,
                    user_id=session.user_id or "demo-user",
                    actor="agent_intake",
                    change_type="removed",
                    constraint_id=old.id,
                    old_value=old.to_dict(),
                )
            except Exception:
                logger.debug("intent intake constraint retire audit failed", exc_info=True)

    added_ids: list[str] = []
    for ec in extracted:
        from app.services.group_purchase.constraints import Constraint

        constraint = Constraint(
            id=f"c_{session_id}_{len(session.constraints) + len(added_ids) + 1}",
            type=ec["type"],
            content=ec["content"],
            hard=ec["hard"],
            source=ec["source"],
            numeric_value=ec["numeric_value"],
            status="active",
        )
        session.constraints.append(constraint)
        added_ids.append(constraint.id)
        try:
            log_constraint_change(
                session_id=session_id,
                user_id=session.user_id or "demo-user",
                actor="agent_intake",
                constraint_id=constraint.id,
                change_type="added",
                new_value=constraint.to_dict(),
            )
        except Exception:
            logger.debug("intent intake constraint audit failed", exc_info=True)

    # 记住「这一批是本次提问自动抽的」，下次新提问只替换这一批（手动约束不受影响）。
    session.auto_constraint_ids = list(added_ids)

    # 3) 这是新的采购任务：让方案重新生成（与约束收紧紧绷快照的语义不冲突）
    session.plan_a = None
    session.plan_b = None
    session.selected_plan = None
    session.plans_snapshot = None
    session.cart_items = []
    session.split_preview = None
    session.order_id = None
    session.order_status = None
    session.state = "constraint_collection"
    update_session(session)

    try:
        log_intent_submitted(
            session_id=session_id,
            user_id=session.user_id or "demo-user",
            actor="agent_intake",
            query=text,
            mode=session.intent_mode,
            extracted_count=len(added_ids),
        )
    except Exception:
        logger.debug("intent audit failed", exc_info=True)

    return {
        "ok": True,
        "query": text,
        "mode": session.intent_mode,
        "extracted_count": len(added_ids),
        "added_ids": added_ids,
        "replaced_count": len(replaced_ids),
        "removed_ids": replaced_ids,
        "constraints_count": sum(
            1 for c in session.constraints if c.status == "active"
        ),
    }


@router.post("/regenerate")
async def regenerate_plans(session_id: str = Query(...)):
    """显式「重新生成方案」：除 /session/reset 外唯一合法的快照失效入口。

    为什么必须有它：GET /plans 是快照语义（约束收紧不会静默改写已选方案，
    守卫在支付时刻把关）。但用户改完约束 / 换口味后需要**主动重算**的能力，
    否则只能刷新页面干瞪眼 —— 这正是"每次推荐都还是上一次的，刷新也没用"的来源。
    """
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not (session.intent_query or "").strip():
        raise HTTPException(status_code=400, detail="还没有采购需求：请先在上方搜索框提交一句话需求")

    session.plan_a = None
    session.plan_b = None
    session.selected_plan = None
    session.plans_snapshot = None
    session.cart_items = []
    session.split_preview = None
    session.order_id = None
    if session.state == "plan_generated":
        session.state = "constraint_collection"
    update_session(session)

    return {
        "ok": True,
        "query": session.intent_query,
        "constraints_count": sum(1 for c in session.constraints if c.status == "active"),
    }


@router.get("")
async def list_plans(session_id: str = Query(...)):
    """Generate dual plans (A=low-price, B=durable) with mandatory budget guardrail check."""
    try:
        session = get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # ── 快照语义（这是设计决策，不是缓存失效语义，请勿"优化"回去）────────────
        # 方案在每个会话内只生成一次（首次 GET），并把完整响应落进 session.plans_snapshot。
        # 此后所有 GET —— 包括 PlansTab 的 5s 轮询 —— 只原样回放快照：不重算、不写审计。
        #
        # 为什么快照是对的（治理叙事因此更硬，而不是更弱）：
        #   "约束收紧不会静默改写你已选的方案；守卫在支付时刻用最新约束把关。"
        #   AI 不悄悄改你的购物车，人做最终决定。若这里改成"约束一变就重算"，那么
        #   加 350 预算后方案 B 会在 5 秒内被静默改写 393→350，购物车随之合规，
        #   支付前守卫（D2 主门）就永远不会触发 —— 整条治理主线在 UI 里消失。
        #
        # 失效方式只有两个：/session/reset（GroupPurchaseSession.reset_to_initial 同款）
        # 或未来的显式"重新生成"动作。别无他途。
        if session.plans_snapshot is not None:
            # constraint_count 是会话元数据而非方案本身，按当前值返回（面板显示不落后）。
            return {
                **session.plans_snapshot,
                "constraint_count": sum(
                    1 for c in (session.constraints or []) if c.status == "active"
                ),
            }

        # 生成期间用户可能又提交了新需求：先记下此刻的检索词，落盘前再比对。
        # 否则一份"上一问"的生成结果会在新 intent 清空快照之后被写回去，
        # 之后所有 GET 都只回放这个过期快照 —— 表现就是"换了问题还是旧方案，必须手动刷新"。
        intent_at_start = session.intent_query

        constraints = session.constraints or []
        
        # Extract budget limit from hard budget constraints
        budget_constraint = next(
            (c for c in constraints if c.type == "budget" and c.status == "active" and c.hard and c.numeric_value),
            None
        )
        budget_limit = budget_constraint.numeric_value if budget_constraint else None

        # Generate dual plans
        result = await generate_dual_plans(
            intent_query=session.intent_query or "ECV 社团采购",
            constraints=constraints,
            min_items=5,
            max_items=8,
            use_channel3=bool(session.intent_query),
        )

        if not result.get("feasible"):
            # 无解也把检索证据带上：赞助商 Channel3 的最接近商品仍要能单独展示，
            # 否则用户只看到「无解」，连"赞助商那边到底找到什么"都无从判断。
            infeasible_meta = result.get("retrieval") or {}
            return {
                "feasible": False,
                "fallback": result.get("fallback"),
                "plans": [],
                "reason": "No feasible plan under constraints",
                "data_source": result.get("source", "k1_catalog"),
                "intent_query": session.intent_query,
                "intent_mode": getattr(session, "intent_mode", "group"),
                "constraint_count": len(constraints),
                "retrieval": {
                    "query": session.intent_query or "ECV 社团采购",
                    "search_query": infeasible_meta.get("search_query") or (session.intent_query or ""),
                    "source": result.get("source", "k1_catalog"),
                    "candidate_count": infeasible_meta.get("candidate_count", 0),
                    "matched_count": infeasible_meta.get("matched_count", 0),
                    "note": infeasible_meta.get("note", ""),
                    "channel3_status": infeasible_meta.get("channel3_status", ""),
                    "sponsor_related": infeasible_meta.get("sponsor_related", []),
                    "sponsor_note": infeasible_meta.get("sponsor_note", ""),
                },
            }

        catalog = get_k1_items()

        # Process Plan A
        plan_a_raw = result.get("plan_a", {})
        plan_a_data = {
            "plan_id": "A",
            "strategy": plan_a_raw.get("strategy", "低价优先"),
            "items": plan_a_raw.get("items", []),
            "total": plan_a_raw.get("total", 0),
            "currency": "USD",
            "matched_constraints": plan_a_raw.get("matched_constraints", []),
            "unmet_constraints": plan_a_raw.get("unmet_constraints", []),
            "trade_offs": plan_a_raw.get("trade_offs", []),
            "risks": plan_a_raw.get("risks", []),
            "source": plan_a_raw.get("source", "k1_catalog"),
        }
        
        # Budget guardrail check for Plan A
        if budget_limit and plan_a_data["items"]:
            guardrail_a = check_budget(
                items=plan_a_data["items"],
                budget_limit=budget_limit,
                allow_substitute=True,
                catalog=catalog,
            )
            plan_a_data["budget_ok"] = guardrail_a["ok"]
            plan_a_data["budget_check"] = {
                "limit": budget_limit,
                "total": guardrail_a["total"],
                "overage": guardrail_a["overage"],
                "suggestions": guardrail_a.get("suggestions", []),
            }
            
            if not guardrail_a["ok"]:
                log_budget_violation(
                    session_id=session_id,
                    user_id=session.user_id or "demo-user",
                    actor="plans_api",
                    budget_limit=budget_limit,
                    actual_total=guardrail_a["total"],
                    resolution=guardrail_a["resolution"],
                )

        # Process Plan B
        plan_b_raw = result.get("plan_b", {})
        plan_b_data = {
            "plan_id": "B",
            "strategy": plan_b_raw.get("strategy", "耐用优先"),
            "items": plan_b_raw.get("items", []),
            "total": plan_b_raw.get("total", 0),
            "currency": "USD",
            "matched_constraints": plan_b_raw.get("matched_constraints", []),
            "unmet_constraints": plan_b_raw.get("unmet_constraints", []),
            "trade_offs": plan_b_raw.get("trade_offs", []),
            "risks": plan_b_raw.get("risks", []),
            "source": plan_b_raw.get("source", "k1_catalog"),
        }
        
        if budget_limit and plan_b_data["items"]:
            guardrail_b = check_budget(
                items=plan_b_data["items"],
                budget_limit=budget_limit,
                allow_substitute=True,
                catalog=catalog,
            )
            plan_b_data["budget_ok"] = guardrail_b["ok"]
            plan_b_data["budget_check"] = {
                "limit": budget_limit,
                "total": guardrail_b["total"],
                "overage": guardrail_b["overage"],
                "suggestions": guardrail_b.get("suggestions", []),
            }
            
            if not guardrail_b["ok"]:
                log_budget_violation(
                    session_id=session_id,
                    user_id=session.user_id or "demo-user",
                    actor="plans_api",
                    budget_limit=budget_limit,
                    actual_total=guardrail_b["total"],
                    resolution=guardrail_b["resolution"],
                )

        # Helper: derive sacrificed members from unmet constraints
        def get_sacrificed_members(plan: dict) -> list[str]:
            member_set = set()
            for cid in plan.get("unmet_constraints", []):
                cons = next((c for c in constraints if c.id == cid), None)
                if cons and cons.source:
                    member_set.add(cons.source)
            return sorted(member_set)

        plans_data = [
            {**plan_a_data, "sacrificed_members": get_sacrificed_members(plan_a_data)},
            {**plan_b_data, "sacrificed_members": get_sacrificed_members(plan_b_data)},
        ]

        # Audit log Plan A generation (optional, skip failure)
        try:
            if plan_a_data["items"]:
                log_plan_generation(
                    session_id=session_id,
                    user_id=session.user_id or "demo-user",
                    actor="plans_api",
                    plan_id="A",
                    items=plan_a_data["items"],
                    total=plan_a_data["total"],
                    trade_offs=plan_a_data.get("trade_offs") or [],
                    risks=plan_a_data.get("risks") or [],
                    all_constraints=[
                        {"id": c.id, "type": c.type, "content": c.content, "source": c.source}
                        for c in constraints
                    ],
                )
        except Exception as log_err:
            logger.debug("Audit log failed for plan generation: %s", log_err)

        retrieval_meta = result.get("retrieval") or {}
        payload = {
            "feasible": True,
            "data_source": result.get("source", "k1_catalog"),
            "plans": plans_data,
            "comparison": result.get("comparison", {}),
            "budget_limit": budget_limit,
            "constraint_count": len(constraints),
            # 检索证据：用户原句 + 实际检索词（Channel3 英文/中英混检）+ 诚实说明。
            "intent_query": session.intent_query,
            "intent_mode": getattr(session, "intent_mode", "group"),
            "retrieval": {
                "query": (session.intent_query or "ECV 社团采购"),
                "search_query": retrieval_meta.get("search_query") or (session.intent_query or ""),
                "source": result.get("source", "k1_catalog"),
                "candidate_count": retrieval_meta.get("candidate_count", 0),
                "matched_count": retrieval_meta.get("matched_count", 0),
                "note": retrieval_meta.get("note", ""),
                "channel3_status": retrieval_meta.get("channel3_status", ""),
                "sponsor_related": retrieval_meta.get("sponsor_related", []),
                "sponsor_note": retrieval_meta.get("sponsor_note", ""),
            },
        }

        # Persist generated plans back into the session so that downstream
        # endpoints (/cart/select_plan, /cart/modify, review panel) can read them.
        # plans_snapshot 保存的是**本函数即将返回的完整响应**：后续 GET 直接回放它，
        # 这样"载荷一致"是靠存下来保证的，而不是靠"planner 恰好是确定性的"。
        try:
            if session.intent_query != intent_at_start:
                # 过期结果：只回给本次请求（前端会按检索词自行丢弃），绝不写进快照
                logger.info(
                    "intent 在生成期间变化（%r → %r），丢弃过期方案、不写快照",
                    intent_at_start,
                    session.intent_query,
                )
            else:
                from app.services.group_purchase.session_state import ShoppingPlan
                session.plan_a = ShoppingPlan.from_dict(plan_a_data)
                session.plan_b = ShoppingPlan.from_dict(plan_b_data)
                session.plans_snapshot = payload
                if session.state == "constraint_collection":
                    session.state = "plan_generated"
                update_session(session)
        except Exception as persist_err:
            logger.warning("Failed to persist plans into session: %s", persist_err)

        return payload

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Plan generation failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {e}")