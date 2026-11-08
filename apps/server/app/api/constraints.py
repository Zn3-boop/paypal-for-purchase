"""Constraints Management APIs - Full Implementation with LLM Extraction.

Single source of truth: session.constraints (in-memory session store).
Conflict detection mirrors ConstraintManager logic but operates on session.constraints.
"""
from __future__ import annotations
import logging
import re
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Literal, List

from app.services.group_purchase.session_state import get_session, update_session
from app.services.group_purchase.constraints import Constraint, BUDGET_CONFLICT_RATIO
from app.services.group_purchase.audit_log import log_constraint_change, log_conflict_detection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/constraints", tags=["constraints"])


class ConstraintCreate(BaseModel):
    type: Literal["budget", "taboo", "preference", "functional"]
    content: str
    source: str
    hard: bool = False
    numeric_value: float | None = None


class ExtractConstraintsRequest(BaseModel):
    text: str
    source: str = "user"


def _detect_conflicts(constraints: list[Constraint]) -> list[dict]:
    """在约束列表上检测冲突（预算差距过大等）。"""
    active = [c for c in constraints if c.status == "active"]
    conflicts = []
    budgets = [c for c in active if c.type == "budget" and c.hard and c.numeric_value]
    for i, a in enumerate(budgets):
        for b in budgets[i + 1:]:
            ratio = max(a.numeric_value, b.numeric_value) / min(a.numeric_value, b.numeric_value)
            if ratio >= BUDGET_CONFLICT_RATIO:
                conflicts.append({
                    "constraint_a": a.to_dict(),
                    "constraint_b": b.to_dict(),
                    "reason": f"预算差距过大：{a.content} vs {b.content}（比例 {ratio:.1f}x）",
                })
    return conflicts


def _group_by_source(constraints: list[Constraint]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for c in constraints:
        grouped.setdefault(c.source, []).append(c.to_dict())
    return grouped


@router.get("")
async def list_constraints(session_id: str = Query(...)):
    """获取当前会话的所有约束及冲突。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # 软删除（status=removed）的约束不再参与看板与下游消费；GET 端即根治过滤。
    constraints = [c for c in session.constraints if c.status == "active"]
    conflicts = _detect_conflicts(constraints)

    return {
        "constraints": [c.to_dict() for c in constraints],
        "conflicts": conflicts,
        "grouped_by_source": _group_by_source(constraints),
    }


@router.post("")
async def add_constraint(session_id: str = Query(...), constraint: ConstraintCreate = None):
    """手动添加一条新约束。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if constraint is None:
        raise HTTPException(status_code=400, detail="Missing constraint body")

    new_constraint = Constraint(
        id=f"c_{session_id}_{len(session.constraints) + 1}",
        type=constraint.type,
        content=constraint.content,
        hard=constraint.hard,
        source=constraint.source,
        numeric_value=constraint.numeric_value,
        status="active",
    )
    session.constraints.append(new_constraint)
    update_session(session)

    conflicts = _detect_conflicts(session.constraints)
    if conflicts:
        log_conflict_detection(
            session_id=session_id,
            user_id=session.user_id or "demo-user",
            actor="constraints_api",
            conflicts=conflicts,
        )

    log_constraint_change(
        session_id=session_id,
        user_id=session.user_id or "demo-user",
        actor="constraints_api",
        constraint_id=new_constraint.id,
        change_type="added",
        new_value=new_constraint.to_dict(),
    )

    return {
        "ok": True,
        "constraint_id": new_constraint.id,
        "conflicts": conflicts,
    }


@router.post("/extract")
async def extract_constraints_from_text(session_id: str = Query(...), req: ExtractConstraintsRequest = None):
    """从自然语言提取结构化约束（LLM 驱动，失败时规则降级）。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    text = req.text if req else ""
    source = req.source if req else "user"
    if not text.strip():
        raise HTTPException(status_code=400, detail="Text is empty")

    extracted = await _extract_with_llm(text, source)

    if not extracted:
        return {"ok": False, "message": "未能提取到任何约束", "extracted_count": 0}

    new_constraints: list[Constraint] = []
    for ec in extracted:
        constraint = Constraint(
            id=f"c_{session_id}_{len(session.constraints) + len(new_constraints) + 1}",
            type=ec["type"],
            content=ec["content"],
            hard=ec["hard"],
            source=ec["source"],
            numeric_value=ec["numeric_value"],
            status="active",
        )
        session.constraints.append(constraint)
        new_constraints.append(constraint)

        log_constraint_change(
            session_id=session_id,
            user_id=session.user_id or "demo-user",
            actor="llm_extractor",
            constraint_id=constraint.id,
            change_type="added",
            new_value=constraint.to_dict(),
        )

    update_session(session)
    conflicts = _detect_conflicts(session.constraints)
    if conflicts:
        log_conflict_detection(
            session_id=session_id,
            user_id=session.user_id or "demo-user",
            actor="constraints_api",
            conflicts=conflicts,
        )

    return {
        "ok": True,
        "extracted_count": len(new_constraints),
        "added_ids": [c.id for c in new_constraints],
        "constraints": [c.to_dict() for c in new_constraints],
        "conflicts": conflicts,
    }


async def _extract_with_llm(text: str, source: str) -> list[dict]:
    """调用 LLM 提取约束；任何失败都降级到规则提取。"""
    try:
        from app.services.llm.client import llm_parse_with_retry

        class ExtractedConstraint(BaseModel):
            constraint_type: Literal["budget", "taboo", "preference", "functional"]
            content: str
            source: str
            hard: bool
            numeric_value: float | None = None

        class ExtractionOutput(BaseModel):
            extracted_constraints: List[ExtractedConstraint]

        prompt = (
            "你是团体采购需求解析器。从下面的自然语言文本中提取结构化约束。\n\n"
            f'文本："{text}"\n\n'
            "约束类型定义：\n"
            "- budget：涉及总金额、预算上限（如\"预算 600\"），hard 一般为 true\n"
            "- taboo：禁忌/排除项（如\"不要塑料\"）\n"
            "- preference：偏好（如\"优先环保\"）\n"
            "- functional：功能性要求（如\"需要 5 人份\"）\n\n"
            f'每条约束的 source 填 "{source}"（除非文本中明确提到别的成员）。\n'
            "如果能从文本提取出数字（金额/人数），填入 numeric_value，否则为 null。\n"
            "只输出 JSON，不要解释。"
        )

        parsed, _ = await llm_parse_with_retry(
            prompt=prompt, resp_model=ExtractionOutput, max_retry=2, timeout=20.0
        )
        if parsed and parsed.extracted_constraints:
            return [
                {
                    "type": ec.constraint_type,
                    "content": ec.content,
                    "source": ec.source or source,
                    "hard": ec.hard,
                    "numeric_value": ec.numeric_value,
                }
                for ec in parsed.extracted_constraints
            ]
    except Exception as e:
        logger.warning("LLM extraction failed (%s), falling back to rules", e)

    return _simple_rule_extraction(text, source)


def _extract_budget_number(text: str) -> float | None:
    """从一句话里提取「预算金额」，而不是第一个出现的数字。

    "帮我找 5 人份的环保桌游奖品，预算 300 美元" → 300，而不是 5。
    策略：优先取「预算/budget/不超过/以内/上限/控制在/只有」等关键词后方最近的数字；
    协商不到就退而求其次——取紧跟"元/美元/块"前的数字；仍无则判定无预算数字。
    """
    try:
        nums = [
            (m.start(), float(m.group(1).replace(",", "")))
            for m in re.finditer(r"(\d[\d,.]*)", text.replace("，", ","))
        ]
    except (ValueError, AttributeError):
        return None
    if not nums:
        return None
    tl = text.lower()
    best: tuple[int, float] | None = None
    for kw in ("预算", "budget", "不超过", "以内", "上限", "控制在", "只有"):
        for km in re.finditer(re.escape(kw), tl):
            for pos, num in nums:
                # 数字必须紧跟关键词之后（允许 ≤ < = 和空格）
                if km.start() <= pos and pos - km.end() <= 4:
                    if best is None or (pos - km.end()) < best[0]:
                        best = (pos - km.end(), num)
    if best is not None:
        return best[1]
    # 兜底：紧跟货币单位之前的数字（"预算 300" / "300 美元" / "300块"）
    currency = re.search(r"(\d[\d,.]*)\s*(元|美元|美刀|块|dollars?|usd)", tl)
    if currency:
        try:
            return float(currency.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _simple_rule_extraction(text: str, source: str) -> list[dict]:
    """规则降级提取。"""
    extracted = []
    tl = text.lower()

    if "预算" in tl or "budget" in tl or "不超过" in tl or "以内" in tl:
        num = _extract_budget_number(text)
        if num is not None:
            extracted.append({
                "type": "budget", "content": f"总预算≤{num}",
                "source": source, "hard": True, "numeric_value": num,
            })

    if any(kw in tl for kw in ["不要", "排除", "禁用", "avoid", "no "]):
        extracted.append({
            "type": "taboo", "content": text, "source": source,
            "hard": False, "numeric_value": None,
        })

    if any(kw in tl for kw in ["优先", "prefer", "喜欢", "希望", "尽量"]):
        extracted.append({
            "type": "preference", "content": text, "source": source,
            "hard": False, "numeric_value": None,
        })

    if any(kw in tl for kw in ["需要", "必须", "must", "require", "功能"]):
        extracted.append({
            "type": "functional", "content": text, "source": source,
            "hard": True, "numeric_value": None,
        })

    return extracted


@router.delete("/{constraint_id}")
async def delete_constraint(session_id: str = Query(...), constraint_id: str = None):
    """删除一条约束（软删除）。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    target = next((c for c in session.constraints if c.id == constraint_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Constraint not found")

    target.status = "removed"
    update_session(session)

    log_constraint_change(
        session_id=session_id,
        user_id=session.user_id or "demo-user",
        actor="constraints_api",
        constraint_id=constraint_id,
        change_type="removed",
    )
    return {"ok": True, "deleted_id": constraint_id}