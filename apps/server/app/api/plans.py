"""Plan Generation APIs with Budget Guardrail Integration."""
from __future__ import annotations
import logging
from fastapi import APIRouter, HTTPException, Query

from app.services.group_purchase.planner import generate_dual_plans
from app.services.group_purchase.session_state import get_session, update_session
from app.services.group_purchase.budget_guardrail import check_budget
from app.services.group_purchase.audit_log import log_plan_generation, log_budget_violation
from app.services.shopper.catalog_k1 import get_k1_items

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("")
async def list_plans(session_id: str = Query(...)):
    """Generate dual plans (A=low-price, B=durable) with mandatory budget guardrail check."""
    try:
        session = get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        constraints = session.constraints or []
        
        # Extract budget limit from hard budget constraints
        budget_constraint = next(
            (c for c in constraints if c.type == "budget" and c.status == "active" and c.hard and c.numeric_value),
            None
        )
        budget_limit = budget_constraint.numeric_value if budget_constraint else None

        # Generate dual plans
        result = await generate_dual_plans(
            intent_query="ECV 社团采购",
            constraints=constraints,
            min_items=5,
            max_items=8,
            use_channel3=False,
        )

        if not result.get("feasible"):
            return {
                "feasible": False,
                "fallback": result.get("fallback"),
                "plans": [],
                "reason": "No feasible plan under constraints",
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

        # Persist generated plans back into the session so that downstream
        # endpoints (/cart/select_plan, /cart/modify, review panel) can read them.
        try:
            from app.services.group_purchase.session_state import ShoppingPlan
            session.plan_a = ShoppingPlan.from_dict(plan_a_data)
            session.plan_b = ShoppingPlan.from_dict(plan_b_data)
            if session.state == "constraint_collection":
                session.state = "plan_generated"
            update_session(session)
        except Exception as persist_err:
            logger.warning("Failed to persist plans into session: %s", persist_err)

        return {
            "feasible": True,
            "data_source": result.get("source", "k1_catalog"),
            "plans": plans_data,
            "comparison": result.get("comparison", {}),
            "budget_limit": budget_limit,
            "constraint_count": len(constraints),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Plan generation failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {e}")