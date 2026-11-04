"""双方案生成引擎 —— 权衡感知（Trade-off Aware）的方案生成。

对应执行清单 C 模块：
  C1 检索层适配：约束作为前置输入传给检索层，不是后置过滤
  C2 双方案生成：A（低价优先）/ B（耐用优先）
  C4 每套方案标注"牺牲了谁的诉求"（成员维度）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from app.services.group_purchase.budget_guardrail import calc_total
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.fallback import generate_fallback_message
from app.services.shopper.catalog_k1 import get_k1_items

logger = logging.getLogger(__name__)

# 关键词抽取辅助
_NEGATION_WORDS = ("不要", "不用", "拒绝", "禁止", "不能", "别用", "不含", "无", "避免", "no ", "without ")
_FILLER_WORDS = ("材质", "产品", "制品", "包装", "的", "东西", "物品")

def _extract_keywords(text: str) -> list[str]:
    """从约束文本中抽取可用于匹配的实体关键词。"""
    if not text:
        return []
    raw = text.strip().lower()
    for sep in ("、", ",", "，", ";", "；", "/", "和", "与"):
        raw = raw.replace(sep, "|")
    parts = [p.strip() for p in raw.split("|") if p.strip()]

    keywords: list[str] = []
    for part in parts:
        cleaned = part
        for neg in _NEGATION_WORDS:
            cleaned = cleaned.replace(neg, "")
        for filler in _FILLER_WORDS:
            cleaned = cleaned.replace(filler, "")
        cleaned = cleaned.strip()
        if cleaned:
            keywords.append(cleaned)
    return keywords


async def _retrieve_catalog(query: str, use_channel3: bool) -> tuple[list[dict[str, Any]], str]:
    """检索商品目录：优先 Channel3，降级本地 catalog。"""
    if use_channel3:
        try:
            from app.services.channel3 import search_products_for_query
            channel3_items = await search_products_for_query(query, limit=20)
            if channel3_items:
                logger.info("Channel3 搜索命中 %d 条商品（query=%r）", len(channel3_items), query)
                return channel3_items, "channel3"
        except Exception as e:
            logger.warning("Channel3 搜索失败：%s，降级本地 catalog", e)

    logger.info("使用本地 catalog 作为后备")
    return get_k1_items(), "k1_catalog"


async def generate_dual_plans(
    intent_query: str,
    constraints: list[Constraint],
    min_items: int = 3,
    max_items: int = 8,
    use_channel3: bool = True,
) -> dict[str, Any]:
    """生成双方案（低价优先 A / 耐用优先 B）。"""
    budget_constraints = [c for c in constraints if c.type == "budget" and c.status == "active"]
    budget_caps = [c.numeric_value for c in budget_constraints if c.numeric_value]
    budget_limit = min(budget_caps) if budget_caps else None

    catalog, source = await _retrieve_catalog(intent_query, use_channel3)
    filtered = prefilter_by_constraints(catalog, constraints)

    if not filtered:
        return {
            "feasible": False,
            "plan_a": None,
            "plan_b": None,
            "source": source,
            "fallback": generate_fallback_message(
                constraints, catalog, [c.id for c in constraints if c.hard], budget_limit
            ),
            "comparison": {},
        }

    plan_a = _assemble_strategy_plan(
        candidates=filtered,
        strategy="cheap",
        budget_limit=budget_limit,
        constraints=constraints,
        max_items=max_items,
        min_items=min_items,
    )
    plan_b = _assemble_strategy_plan(
        candidates=filtered,
        strategy="quality",
        budget_limit=budget_limit,
        constraints=constraints,
        max_items=max_items,
        min_items=min_items,
    )

    # J 模块：生成后二次检查——任一方案为空说明无解，触发兜底
    if not plan_a["items"] or not plan_b["items"]:
        return {
            "feasible": False,
            "plan_a": None,
            "plan_b": None,
            "source": source,
            "fallback": generate_fallback_message(constraints, filtered, [], budget_limit),
            "comparison": {},
        }

    return {
        "feasible": True,
        "plan_a": plan_a,
        "plan_b": plan_b,
        "source": source,
        "comparison": _compare_plans(plan_a, plan_b, budget_limit),
    }


def _assemble_strategy_plan(
    candidates: list[dict[str, Any]],
    strategy: Literal["cheap", "quality"],
    budget_limit: float | None,
    constraints: list[Constraint],
    max_items: int = 8,
    min_items: int = 3,
) -> dict[str, Any]:
    """按策略从候选商品中挑选商品，组装一个方案。"""
    plan_id = "A" if strategy == "cheap" else "B"
    strategy_name = "低价优先" if strategy == "cheap" else "耐用优先"

    sorted_candidates = sorted(
        candidates,
        key=lambda c: c.get("price", 0),
        reverse=(strategy == "quality"),
    )

    selected: list[dict[str, Any]] = []
    spent = 0.0

    for item in sorted_candidates:
        price = item.get("price", 0)
        if budget_limit is not None and spent + price > budget_limit:
            continue
        selected.append(item)
        spent += price
        if len(selected) >= max_items:
            break

    # 若商品太少，尝试补充最便宜的
    if len(selected) < min_items:
        selected_ids = {it["id"] for it in selected}
        for item in sorted(candidates, key=lambda c: c.get("price", 0)):
            if item["id"] in selected_ids:
                continue
            price = item.get("price", 0)
            if budget_limit is not None and spent + price > budget_limit:
                continue
            selected.append(item)
            spent += price
            selected_ids.add(item["id"])
            if len(selected) >= min_items:
                break

    # 构建 PlanItem 列表
    items = []
    for item in selected:
        source_val = item.get("source", "local_catalog")
        items.append({
            "id": item["id"],
            "name": item["name"],
            "category": item.get("category", ""),
            "price": item.get("price", 0),
            "quantity": 1,
            "subtotal": item.get("price", 0),
            "matched_constraints": [],
            "source": source_val,
            "rationale": "",
        })

    total = calc_total(items)

    matched = []
    unmet = []
    for c in constraints:
        if c.status != "active":
            continue
        if _check_constraint_match(item_list=items, constraint=c):
            matched.append(c.id)
        else:
            unmet.append(c.id)

    trade_offs = _generate_trade_offs(strategy_name, constraints, unmet)
    risks = _generate_risks(strategy_name, total, budget_limit)

    return {
        "plan_id": plan_id,
        "strategy": strategy_name,
        "items": items,
        "total": round(total, 2),
        "currency": "USD",
        "matched_constraints": matched,
        "unmet_constraints": unmet,
        "trade_offs": trade_offs,
        "risks": risks,
    }


def _check_constraint_match(item_list: list[dict], constraint: Constraint) -> bool:
    """检查选中的商品组合是否满足某条约束。"""
    ctype = constraint.type
    ccontent = (constraint.content or "").lower()
    tags_blob = " ".join(str(t).lower() for it in item_list for t in (it.get("tags", []) or []))
    names_blob = " ".join(it["name"].lower() for it in item_list)

    if ctype == "budget":
        return True

    if ctype == "taboo":
        for keyword in _extract_keywords(ccontent) or [k.strip() for k in ccontent.split("、") if k.strip()]:
            if keyword in tags_blob or keyword in names_blob:
                return False
        return True

    if ctype == "functional":
        for keyword in _extract_keywords(ccontent) or [k.strip() for k in ccontent.split("、") if k.strip()]:
            if keyword in tags_blob or keyword in names_blob:
                return True
        return False

    return True


def _generate_trade_offs(strategy_name: str, constraints: list[Constraint], unmet_constraints: list[str]) -> list[str]:
    """生成方案的取舍说明。"""
    trades = []
    if unmet_constraints:
        trades.append(f"未完全满足 {len(unmet_constraints)} 条约束")

    member_prefs: dict[str, list[str]] = {}
    for c in constraints:
        if c.status != "active" or c.type != "preference":
            continue
        member_prefs.setdefault(c.source, []).append(c.content)

    for source, prefs in member_prefs.items():
        trades.append(f"牺牲了 {source} 的偏好：{', '.join(prefs)}")

    if strategy_name == "低价优先":
        trades.append("选择了平价款，可能在耐用性和品牌上有所妥协")
    else:
        trades.append("选择了高品质款，总成本相对较高")

    return trades


def _generate_risks(strategy_name: str, total: float, budget_limit: float | None) -> list[str]:
    """生成潜在风险提示。"""
    risks = []
    if budget_limit is not None and total > budget_limit * 0.95:
        risks.append(f"总价 {total:.2f} 接近预算上限 {budget_limit:.2f}，价格波动可能导致超支")

    if strategy_name == "耐用优先":
        risks.append("高品质商品价格较高，若后续需删减物料，预算压力会增大")

    risks.append("最终支付需用户在 PayPal 页面点击确认，AI 无支付发起权")
    return risks


def _compare_plans(plan_a: dict, plan_b: dict, budget_limit: float | None) -> dict:
    """比较两个方案的差异。"""
    return {
        "total_delta": round(plan_b["total"] - plan_a["total"], 2),
        "item_count_a": len(plan_a["items"]),
        "item_count_b": len(plan_b["items"]),
        "budget_limit": budget_limit,
        "recommendation": (
            "建议方案 A（低价优先），适合预算敏感场景"
            if plan_a["total"] <= (budget_limit or float("inf")) * 0.9
            else "建议方案 B（耐用优先），耐用性更好"
        ),
    }


def prefilter_by_constraints(catalog: list[dict[str, Any]], constraints: list[Constraint]) -> list[dict[str, Any]]:
    """C1：基于硬约束前置过滤商品目录。"""
    if not catalog:
        return []

    hard_constraints = [c for c in constraints if c.status == "active" and c.hard]
    if not hard_constraints:
        return list(catalog)

    filtered: list[dict[str, Any]] = []

    # Quantity-type functional constraints (e.g. "5 人份") describe cart-level needs, not
    # item attributes. Filtering items by them empties the whole catalog. Handle them at
    # plan-assembly time instead.
    functional_constraints = [c for c in hard_constraints if c.type == "functional"]
    # Quantity-type constraints may be phrased many ways by the LLM extractor
    # ("5 人份", "社团 5 人", "need 5 sets", "5-piece"). Treat any functional
    # constraint carrying a number as cart-level quantity info: it describes how
    # much to buy, not which item attributes are required.
    quantity_only = all(
        _is_quantity_constraint(c)
        for c in functional_constraints
    ) if functional_constraints else False

    for item in catalog:
        item_blob = " ".join(str(item.get(k, "")) for k in ("name", "description", "category")).lower()
        tags_blob = " ".join(str(t).lower() for t in (item.get("tags", []) or []))
        full_blob = f"{item_blob} {tags_blob}"

        passed = True
        for c in hard_constraints:
            if c.type == "budget":
                if c.numeric_value is not None and item.get("price", 0) > c.numeric_value:
                    passed = False
                    break
            elif c.type == "taboo":
                keywords = _extract_keywords(c.content)
                if keywords:
                    for kw in keywords:
                        if kw in full_blob:
                            passed = False
                            break
                if not passed:
                    break
            elif c.type == "functional" and not quantity_only:
                keywords = _extract_keywords(c.content) or [k.strip() for k in c.content.split("、") if k.strip()]
                if keywords:
                    matched = any(kw in full_blob for kw in keywords)
                    if not matched:
                        passed = False
                        break

        if passed:
            filtered.append(item)

    return filtered


def _is_quantity_constraint(c: Constraint) -> bool:
    """功能约束是否为数量/人数类（购物车层面信息，不应过滤单品）。"""
    text = (c.content or "").lower()
    if c.numeric_value is not None and c.numeric_value <= 100:
        return True
    return any(
        kw in text for kw in ("人份", "person", "people", "qty", "quantity", "份", "人数", "个", "人", "set")
    )