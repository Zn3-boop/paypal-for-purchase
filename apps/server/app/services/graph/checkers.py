"""Small deterministic safety checkers used by the commerce graph."""

from __future__ import annotations

from typing import Any

from app.services.graph.state import AgentState


def fast_brain_node(state: AgentState) -> dict[str, Any]:
    """快脑最小实现：直接放行到慢脑意图识别（不调用任何本地 LLM / Ollama）。"""
    return {
        "fast_brain_decision": "proceed",
        "fast_brain_mode": "full_slow",
        "fast_brain_msg": "checker: fast_brain pass-through",
        "fast_brain_result": "",
        "fast_brain_confidence": 1.0,
        "fast_brain_intent": state.get("intent", ""),
    }


def guard_pre_check_node(state: AgentState) -> dict[str, Any]:
    """前置守卫最小实现：直接放行（last_error 置空，route_after_guard → risk_check）。"""
    return {"last_error": None}


def risk_check_node(state: AgentState) -> dict[str, Any]:
    """风险检查最小实现：默认低风险、无需人工审批。"""
    return {
        "risk_level": "low",
        "risk_reason": "checker: default low risk",
        "approval_required": False,
    }


def post_validate_gate(state: AgentState) -> dict[str, Any]:
    """工具后置校验最小实现：无错误放行（route_after_validate → memory_persist）。"""
    return {"last_error": None}
