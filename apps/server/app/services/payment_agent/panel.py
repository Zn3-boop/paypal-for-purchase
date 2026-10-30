"""可解释性面板数据源 —— 把散落的证据拼成前端可展开的证据结构。

数据源（全部来自 AgentState / payment 子图产物）：
  - authority.reason / llm_reason     授权裁决 + LLM 理由
  - shopping_rank_source / match_tags 选品依据 + 命中标签
  - trust_updated / decision_trace    信任分回写 + 决策留痕

输出结构（前端渲染契约）：
  {
    item, amount, currency,
    rank_source, match_tags, anchors,
    authority: {verdict, rule, reason, llm_reason, risk_level, trust_score},
    approval:  {status, comment, evidence_applied},
    payment:   {mode, order_id, capture_id, status, msg, approval_url, error},
    trust:     {old_score, new_score, evidence_grade, evidence_claim},
    trace:     decision_trace,
    explanation,
    llm_source,
  }
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 拼装入口
# ---------------------------------------------------------------------------


def assemble_explainability_panel(state: dict[str, Any]) -> dict[str, Any]:
    """从 AgentState 拼装前端可解释性面板数据（永不抛异常，缺字段留空）。"""
    try:
        authority = state.get("authority", {}) or {}
        selected = state.get("shopping_selected", {}) or {}
        trust_updated = state.get("trust_updated", {}) or {}
        approval_evidence = state.get("approval_evidence_applied", {}) or {}
        trace = state.get("decision_trace", {}) or {}

        return {
            "item": {
                "name": selected.get("name", ""),
                "price": selected.get("price"),
                "currency": selected.get("currency", "USD"),
                "category": selected.get("category", ""),
"image_url": selected.get("image_url", ""),
                "description": selected.get("description", ""),
            },
            "rank_source": state.get("shopping_rank_source", ""),
            "match_tags": list(selected.get("match_tags", []) or state.get("shopping_match_tags", []) or []),
            "anchors": list(state.get("shopping_anchors", []) or []),
            "authority": {
                "verdict": authority.get("verdict", ""),
                "rule": authority.get("rule", ""),
                "reason": authority.get("reason", ""),
                "llm_reason": authority.get("llm_reason", ""),
                "risk_level": authority.get("risk_level", ""),
                "trust_score": authority.get("trust_score"),
                "goal_conflicts": list(authority.get("goal_conflicts", []) or []),
                "impulse_signals": list(
                    (authority.get("impulse", {}) or {}).get("signals", []) or []
                ),
                "llm_source": authority.get("llm_source", ""),
            },
            "approval": {
                "status": state.get("approval_status", ""),
                "comment": state.get("approval_comment", ""),
                "evidence_applied": {
                    "claim": approval_evidence.get("claim", ""),
                    "new_trust_score": approval_evidence.get("new_trust_score"),
                },
            },
            "payment": {
                "mode": state.get("paypal_mode", ""),
                "order_id": state.get("paypal_order_id", ""),
                "capture_id": state.get("capture_id", ""),
                "status": state.get("paypal_status", ""),
                "msg": state.get("paypal_msg", ""),
                "approval_url": state.get("approval_url", ""),
                "error": state.get("paypal_error", ""),
                "source": state.get("paypal_source", ""),
                "intent_id": state.get("paypal_intent_id", ""),
                "evidence_caveats": list(state.get("evidence_caveats", []) or []),
            },
            "trust": {
                "old_score": trust_updated.get("old_score"),
                "new_score": trust_updated.get("new_score"),
                "evidence_grade": trust_updated.get("evidence_grade", ""),
                "evidence_claim": trust_updated.get("evidence_claim", ""),
            },
            "trace": {
                "order_id": trace.get("order_id", ""),
                "capture_id": trace.get("capture_id", ""),
                "status": trace.get("status", ""),
                "source": trace.get("source", ""),
                "ts": trace.get("ts", ""),
            },
            "explanation": state.get("explanation", {}),
            "llm_source": state.get("llm_source", ""),
        }
    except Exception:  # pragma: no cover - 面板数据源绝不硬失败
        return {}


def evidence_groups_for_panel(state: dict[str, Any]) -> list[dict[str, Any]]:
    """生成可展开的证据分组（前端折叠面板用）。"""
    panel = assemble_explainability_panel(state)
    groups: list[dict[str, Any]] = []

    authority = panel.get("authority", {})
    if authority.get("reason") or authority.get("llm_reason"):
        groups.append(
            {
                "title": "为什么这样授权",
                "kind": "authority",
                "rows": [
                    {"label": "裁决", "value": authority.get("verdict", "")},
                    {"label": "规则", "value": authority.get("rule", "")},
                    {"label": "规则理由", "value": authority.get("reason", "")},
                    {"label": "LLM 理由", "value": authority.get("llm_reason", "")},
                    {"label": "风险", "value": authority.get("risk_level", "")},
                    {"label": "信任分", "value": _fmt(authority.get("trust_score"))},
                    {"label": "目标冲突", "value": "；".join(authority.get("goal_conflicts", []) or [])},
                    {"label": "冲动信号", "value": "；".join(authority.get("impulse_signals", []) or [])},
                ],
            }
        )

    if panel.get("rank_source") or panel.get("match_tags"):
        groups.append(
            {
                "title": "为什么选这个",
                "kind": "selection",
                "rows": [
                    {"label": "排序来源", "value": panel.get("rank_source", "")},
                    {"label": "命中标签", "value": "；".join(panel.get("match_tags", []) or [])},
                    {"label": "排序锚点", "value": "；".join(panel.get("anchors", []) or [])},
                ],
            }
        )

    payment = panel.get("payment", {})
    if payment.get("order_id") or payment.get("capture_id"):
        groups.append(
            {
                "title": "支付凭证",
                "kind": "payment",
                "rows": [
                    {"label": "模式", "value": payment.get("mode", "")},
                    {"label": "订单号", "value": payment.get("order_id", "")},
                    {"label": "捕获号", "value": payment.get("capture_id", "")},
                    {"label": "状态", "value": payment.get("status", "")},
                    {"label": "消息", "value": payment.get("msg", "")},
                    {"label": "异常降级", "value": payment.get("error", "")},
                ],
            }
        )

    trust = panel.get("trust", {})
    if trust.get("old_score") is not None or trust.get("new_score") is not None:
        groups.append(
            {
                "title": "信任分变化",
                "kind": "trust",
                "rows": [
                    {"label": "原信任分", "value": _fmt(trust.get("old_score"))},
                    {"label": "新信任分", "value": _fmt(trust.get("new_score"))},
                    {"label": "证据级别", "value": trust.get("evidence_grade", "")},
                    {"label": "证据声明", "value": trust.get("evidence_claim", "")},
                ],
            }
        )

    return groups


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)