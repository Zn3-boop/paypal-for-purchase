"""兜底逻辑 —— 全方案违反硬约束时的提示与二选一建议。

对应执行清单 J 模块：
  J1 全方案违反硬约束时：输出「无法满足 + 二选一建议」
  J2 兜底文案走统一模板，确保演示可控
"""
from __future__ import annotations

from typing import Any

from app.services.group_purchase.constraints import Constraint


def generate_fallback_message(
    constraints: list[Constraint],
    catalog: list[dict[str, Any]],
    filtered_out: list[str],
    budget_limit: float | None,
) -> dict[str, Any]:
    """J1/J2: 生成兜底提示。
    
    返回:
    {
        "ok": False,  # 始终为 False
        "status": "no_feasible_plan",
        "message": str,      # 人类可读的主提示
        "reason": str,       # 具体原因分类
        "suggestions": [     # 二选一建议列表
            {"action": str, "detail": str, "estimated_impact": str},
        ]
    }
    """
    active = [c for c in constraints if c.status == "active"]
    taboos = [c for c in active if c.type == "taboo" and c.hard]
    budgets = [c for c in active if c.type == "budget" and c.hard]
    
    if not filtered_out and catalog:
        # 不应该走到这里——说明调用有误
        return {
            "ok": False,
            "status": "internal_error",
            "message": "系统内部错误：筛选后仍有商品但未能生成方案",
            "reason": "unexpected",
            "suggestions": [],
        }
    
    if taboos:
        # 禁忌冲突：所有商品都含禁忌关键词
        taboo_str = "、".join([c.content for c in taboos])
        suggestions = [
            {
                "action": "解除禁忌约束",
                "detail": f"解除以下禁忌：{taboo_str}",
                "estimated_impact": "可恢复全部商品选择范围",
            },
            {
                "action": "放宽禁忌表述",
                "detail": "例如将「不要任何塑料制品」改为「尽量避免一次性塑料\"",
                "estimated_impact": "保留部分环保考量，同时扩大选择",
            },
        ]
        
        return {
            "ok": False,
            "status": "hard_constraint_violation",
            "message": f"⚠️ 当前禁忌约束无法满足，无法生成可行方案。\n\n禁忌要求：{taboo_str}\n\n所有候选商品均包含禁忌元素。",
            "reason": "taboo_violation",
            "suggestions": suggestions,
        }
    
    if budgets and catalog:
        # Budget-related emptiness: only "too low" when even the cheapest item exceeds budget.
        min_price = min(float(i.get("price", 999999)) for i in catalog)
        effective_budget = min(
            (c.numeric_value for c in budgets if c.numeric_value is not None),
            default=None,
        )
        if effective_budget is not None and min_price <= effective_budget:
            # Cheapest item fits, so the empty result came from other filters, not the budget.
            # Bug 3（评审）：真实的常见原因是「候选目录没覆盖该品类」——K1 示例目录只有
            # 45 件（保温瓶/桌游/帆布包），没有键盘/牛仔裤/围巾；此时让用户"调整约束"
            # 等于往错误方向引。文案诚实化：直说目录可能没覆盖，并给出可操作的两个方向。
            return {
                "ok": False,
                "status": "no_feasible_plan",
                "message": (
                    "⚠️ 当前约束下没有可行组合。\n\n"
                    "预算本身并不算紧，问题更可能出在：\n"
                    "· 当前商品目录未覆盖所需品类（本地示例目录仅含 45 件示例商品，"
                    "不含键盘/牛仔裤/围巾等品类属正常）\n"
                    "· 其他硬约束（禁忌/功能）过严\n\n"
                    "可点「🔄 重新生成方案」让系统换一批检索词重试，"
                    "或切换 Channel3 检索真实商品目录。"
                ),
                "reason": "constraint_conflict",
                "suggestions": [
                    {"action": "重新生成方案", "detail": "换英文检索词重试（系统会先打英文关键词再打中文原句）", "estimated_impact": "可能命中 Channel3 真实商品"},
                    {"action": "放宽禁忌/功能约束", "detail": "暂时将某条硬约束改为软约束", "estimated_impact": "扩大可选范围"},
                ],
            }
        suggestions = [
            {
                "action": "提高预算",
                "detail": f"当前最低单价 {min_price:.2f}，建议预算至少提升至 {min_price:.2f}",
                "estimated_impact": "可选择单件商品",
            },
            {
                "action": "减少采购数量",
                "detail": "若只需 1 件，当前预算已足够",
                "estimated_impact": "维持预算不变",
            },
        ]
        
        return {
            "ok": False,
            "status": "budget_too_low",
            "message": f"⚠️ 预算过低，连单件商品都无法满足。\n\n当前预算：{budget_limit:.2f}\n最低商品单价：{min_price:.2f}",
            "reason": "insufficient_budget",
            "suggestions": suggestions,
        }
    
    # 通用兜底：未知原因无解
    return {
        "ok": False,
        "status": "no_feasible_plan",
        "message": "⚠️ 在当前约束条件下，找不到可行的商品组合。\n\n可能的原因：\n- 约束过于严格\n- 商品目录与需求不匹配\n\n请调整约束后重试。",
        "reason": "unknown",
        "suggestions": [
            {"action": "放宽预算", "detail": "尝试将预算提高 20-30%", "estimated_impact": "扩大可选范围"},
            {"action": "移除软约束", "detail": "暂时放宽偏好类要求", "estimated_impact": "优先满足硬约束"},
        ],
    }


def suggest_constraint_relaxation(
    constraints: list[Constraint],
    budget_limit: float | None,
) -> dict[str, Any]:
    """给出约束放松的具体建议（用于兜底提示）。"""
    suggestions = []
    
    budget_con = [c for c in constraints if c.type == "budget" and c.status == "active"]
    if budget_con:
        current = min([c.numeric_value for c in budget_con if c.numeric_value], default=1000)
        suggestions.append({
            "constraint_id": budget_con[0].id,
            "suggestion": f"将预算上限从 {current:.2f} 提高至 {current * 1.3:.2f}（提升 30%）",
            "new_value": round(current * 1.3, 2),
        })
    
    taboo_con = [c for c in constraints if c.type == "taboo" and c.hard]
    if taboo_con:
        suggestions.append({
            "constraint_id": taboo_con[0].id,
            "suggestion": f"将「{taboo_con[0].content}」改为软性约束（可妥协）",
            "new_hard": False,
        })
    
    return {"suggestions": suggestions}