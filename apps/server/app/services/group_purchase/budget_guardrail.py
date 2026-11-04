"""预算 Guardrail —— 强制预算约束检查与超预算处理策略。

对应执行清单 D 模块：
  D1 calc_total() 总价计算
  D2 check_budget() 强制调用三处
  D3 超预算策略：替换平价款或明确提示
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def calc_total(items: list[dict[str, Any]]) -> float:
    """D1: 计算购物车总价。
    
    支持多种商品结构：
    - {price: number}
    - {price: number, quantity: int} (默认 quantity=1)
    """
    total = 0.0
    for item in items:
        price = float(item.get("price", 0) or 0)
        quantity = int(item.get("quantity", 1) or 1)
        total += price * quantity
    return round(total, 2)


def check_budget(
    items: list[dict[str, Any]],
    budget_limit: float,
    allow_substitute: bool = True,
    catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """D2: 强制预算检查。
    
    返回:
    {
        "ok": bool,           # 是否在预算内
        "total": float,       # 当前总价
        "budget": float,      # 预算上限
        "overage": float,     # 超支金额（负数表示结余）
        "suggestions": [],    # 超支时的替代建议
        "resolution": str     # 推荐处理方式
    }
    """
    total = calc_total(items)
    overage = total - budget_limit
    ok = overage <= 0
    
    result = {
        "ok": ok,
        "total": total,
        "budget": budget_limit,
        "overage": round(overage, 2),
        "suggestions": [],
        "resolution": ""
    }
    
    if not ok:
        # D3: 超预算处理策略
        if allow_substitute and catalog:
            suggestions = find_cheaper_alternatives(items, catalog, overage)
            if suggestions:
                result["suggestions"] = suggestions
                result["resolution"] = f"检测到超支 {overage:.2f}。可尝试替换为平价款：{len(suggestions)} 个建议。"
            else:
                result["resolution"] = f"超支 {overage:.2f}，无法找到合适的平价款。请考虑放宽预算或删除部分商品。"
        else:
            result["resolution"] = f"超支 {overage:.2f}，已超过预算上限 {budget_limit}。"
    else:
        result["resolution"] = f"预算充足，结余 {(budget_limit - total):.2f}。"
    
    return result


def find_cheaper_alternatives(
    current_items: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    target_savings: float,
) -> list[dict[str, Any]]:
    """D3: 寻找更便宜的替代品。
    
    策略：
    1. 对每件商品，在目录中寻找同品类更低价格的选项
    2. 优先替换价格最高且能找到明显差价的商品
    3. 返回能节省 target_savings 的最少替换组合
    """
    alternatives = []
    
    for current_item in current_items:
        current_price = float(current_item.get("price", 0) or 0)
        category = current_item.get("category", "")
        tags = current_item.get("tags", [])
        
        # 寻找同品类更便宜的选项
        cheaper_options = []
        for candidate in catalog:
            if candidate.get("id") == current_item.get("id"):
                continue
            if candidate.get("category") != category:
                continue
            
            candidate_price = float(candidate.get("price", 0) or 0)
            if candidate_price >= current_price:
                continue
            
            saving = current_price - candidate_price
            cheaper_options.append({
                "original_item": current_item.get("name"),
                "replacement": candidate.get("name"),
                "original_price": current_price,
                "replacement_price": candidate_price,
                "savings": round(saving, 2),
                "reason": candidate.get("rationale", "类似品类，价格更低"),
            })
        
        # 按节省金额排序，取最佳选项
        if cheaper_options:
            cheaper_options.sort(key=lambda x: x["savings"], reverse=True)
            alternatives.append(cheaper_options[0])
    
    # 贪心算法：选择能最快达到 target_savings 的替换
    alternatives.sort(key=lambda x: x["savings"], reverse=True)
    
    total_savings = 0.0
    selected = []
    for alt in alternatives:
        selected.append(alt)
        total_savings += alt["savings"]
        if total_savings >= target_savings * 0.8:  # 80% 即可
            break
    
    return selected


def adjust_cart_to_budget(
    items: list[dict[str, Any]],
    budget_limit: float,
    catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """智能调整购物车至预算内。
    
    返回:
    {
        "adjusted_items": [],  # 调整后的商品列表
        "new_total": float,
        "removed_items": [],   # 被移除的商品
        "replaced_items": [],  # 被替换的商品
        "still_over": bool,    # 是否仍超预算
        "message": str         # 人类可读的调整说明
    }
    """
    import copy
    
    adjusted = copy.deepcopy(items)
    removed = []
    replaced = []
    
    # 第一轮：尝试替换
    if catalog:
        check = check_budget(adjusted, budget_limit, allow_substitute=True, catalog=catalog)
        if not check["ok"] and check["suggestions"]:
            for suggestion in check["suggestions"]:
                # 找到对应商品并替换
                for i, item in enumerate(adjusted):
                    if item.get("name") == suggestion["original_item"]:
                        # 在 catalog 中找到替代品
                        for cand in catalog:
                            if cand.get("name") == suggestion["replacement"]:
                                replaced.append({
                                    "original": item.get("name"),
                                    "replacement": suggestion["replacement"],
                                    "savings": suggestion["savings"],
                                })
                                adjusted[i] = {**cand, "quantity": item.get("quantity", 1)}
                                break
                        break
    
    # 第二轮：如果仍超预算，逐步移除最低优先级商品
    check = check_budget(adjusted, budget_limit, allow_substitute=False)
    if not check["ok"]:
        # 按价格从低到高移除（优先保留高价重要商品）
        sorted_items = sorted(adjusted, key=lambda x: x.get("price", 0))
        for item in sorted_items:
            if check["ok"]:
                break
            adjusted.remove(item)
            removed.append(item.get("name"))
            check = check_budget(adjusted, budget_limit, allow_substitute=False)
    
    new_total = calc_total(adjusted)
    still_over = new_total > budget_limit
    
    message_parts = []
    if replaced:
        message_parts.append(f"替换了 {len(replaced)} 件商品为平价款")
    if removed:
        message_parts.append(f"移除了 {len(removed)} 件商品")
    if still_over:
        message_parts.append(f"调整后仍超支 {(new_total - budget_limit):.2f}，请进一步调整")
    else:
        message_parts.append(f"已调整至预算内，当前总价 {new_total:.2f}")
    
    return {
        "adjusted_items": adjusted,
        "new_total": new_total,
        "removed_items": removed,
        "replaced_items": replaced,
        "still_over": still_over,
        "message": "；".join(message_parts),
    }