"""B（匹配优先）的替换规则回归 —— 反腐化四条铁律。

旧实现「同分也优先换更贵的」，于是 B 的价格被算法保证单调上升（8/8 全换、共有 0 件）。
契约（改动这些等于改产品规则）：
  1) 只有「匹配分严格更高」才算升级；同分不换。
  2) 只有 tier ≤ T3（同一商品 / 同款异规格 / 同品牌）才允许替换。
  3) 单件涨幅 ≤ 50%，整篮涨幅 ≤ 30%，最多替换 3 件。
  4) 一条可辩护替换都没有时，B 声明自己是「独立按匹配分重排」，不假装升级。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.planner import (  # noqa: E402
    DEFAULT_MAX_UPGRADES,
    _compare_plans,
    _upgrade_basis_items,
)


def _vinyl(item_id: str, core: str, fmt: str, price: float) -> dict:
    """同款异规格：core 相同、format 不同 → T1（允许替换的最典型形态）。"""
    return {
        "id": item_id,
        "name": f"{core} - {fmt} Single",
        "category": "Vinyl",
        "tags": [f"{fmt}-inch single"],
        "price": price,
    }


def _rel(scores: dict[str, float]) -> dict[str, dict]:
    return {item_id: {"score": score} for item_id, score in scores.items()}


def test_equal_score_never_buys_the_more_expensive_item():
    base = _vinyl("a1", "Winston Theme", "7-Inch", 10.0)
    pricier = _vinyl("b1", "Winston Theme", "12-Inch", 20.0)
    selected, _ = _upgrade_basis_items(
        basis_items=[base], candidates=[base, pricier],
        relevance=_rel({"a1": 3, "b1": 3}), budget_limit=1000.0, max_items=8,
    )
    assert [it["id"] for it in selected] == ["a1"], "同分不得替换成更贵的那件"


def test_strictly_higher_score_upgrades_even_if_cheaper():
    base = _vinyl("a1", "Winston Theme", "7-Inch", 40.0)
    better_cheaper = _vinyl("b1", "Winston Theme", "12-Inch", 12.0)
    selected, _ = _upgrade_basis_items(
        basis_items=[base], candidates=[base, better_cheaper],
        relevance=_rel({"a1": 1, "b1": 5}), budget_limit=1000.0, max_items=8,
    )
    assert [it["id"] for it in selected] == ["b1"]


def test_same_kind_only_candidates_are_rejected():
    """仅同类型（T5）不得替换：这就是「funk 单曲换成 blues 合集」的拦截点。"""
    base = {"id": "a1", "name": "Poon Tang Thump - 7-Inch Single", "category": "Vinyl",
            "tags": ["7-inch single"], "price": 12.0}
    wrong = {"id": "b1", "name": "Jimmy Reed At Carnegie Hall", "category": "Vinyl",
             "tags": ["blues"], "price": 57.0}
    selected, _ = _upgrade_basis_items(
        basis_items=[base], candidates=[base, wrong],
        relevance=_rel({"a1": 1, "b1": 9}), budget_limit=10_000.0, max_items=8,
    )
    assert [it["id"] for it in selected] == ["a1"]


def test_price_uplift_cap_blocks_overpriced_substitute():
    base = _vinyl("a1", "Winston Theme", "7-Inch", 10.0)
    too_expensive = _vinyl("b1", "Winston Theme", "12-Inch", 16.0)  # +60%
    selected, _ = _upgrade_basis_items(
        basis_items=[base], candidates=[base, too_expensive],
        relevance=_rel({"a1": 1, "b1": 5}), budget_limit=10_000.0, max_items=8,
    )
    assert [it["id"] for it in selected] == ["a1"]
def test_upgrades_are_capped_so_most_items_stay_shared():
    """最多换 3 件：B 是局部替换，不是另一份完全不同的清单。"""
    candidates: list[dict] = []
    basis: list[dict] = []
    scores: dict[str, float] = {}
    for i in range(5):
        cheap = _vinyl(f"a{i}", f"Track {i}", "7-Inch", 10.0)
        better = _vinyl(f"b{i}", f"Track {i}", "12-Inch", 12.0)
        candidates += [cheap, better]
        basis.append(dict(cheap))
        scores[f"a{i}"], scores[f"b{i}"] = 1, 5

    selected, _ = _upgrade_basis_items(
        basis_items=basis, candidates=candidates, relevance=_rel(scores),
        budget_limit=10_000.0, max_items=8,
    )
    ids = [it["id"] for it in selected]
    assert ids == ["b0", "b1", "b2", "a3", "a4"], ids
    assert sum(1 for i in ids if i.startswith("b")) == DEFAULT_MAX_UPGRADES
    assert len([i for i in ids if i.startswith("a")]) >= 2


def test_upgrade_falls_back_when_budget_would_be_exceeded():
    """预算装不下就保留 A 的原件：B 不会为了「更贴题」而超预算。"""
    base = _vinyl("a1", "Winston Theme", "7-Inch", 400.0)
    better = _vinyl("b1", "Winston Theme", "12-Inch", 500.0)  # +25%，过得了价格门
    selected, _ = _upgrade_basis_items(
        basis_items=[base], candidates=[base, better],
        relevance=_rel({"a1": 1, "b1": 9}), budget_limit=450.0, max_items=8,
    )
    assert [it["id"] for it in selected] == ["a1"]


def _plan(plan_id: str, rows: list[tuple[str, float, float]], mode: str = "independent") -> dict:
    return {
        "plan_id": plan_id,
        "selection_mode": mode,
        "items": [
            {"id": f"{plan_id}-{name}", "name": name, "price": price, "match_score": score}
            for name, price, score in rows
        ],
        "total": round(sum(price for _, price, _ in rows), 2),
    }


def test_recommendation_is_neutral_and_admits_price_direction():
    """不许「替用户选 A」，也不许把更贵说成更好。"""
    plan_a = _plan("A", [("p1", 10.0, 1.0), ("p2", 5.0, 1.0)])
    plan_b = _plan("B", [("q1", 14.0, 5.0), ("p2", 5.0, 1.0)], mode="substitution")
    comparison = _compare_plans(plan_a, plan_b, budget_limit=100.0, max_upgrades=3)

    rec = comparison["recommendation"]
    assert "建议方案 A" not in rec, rec
    assert "互不占优" in rec, rec
    assert "不代表更耐用" in rec, rec
    assert str(len(comparison["upgrades"])) in rec, rec
    assert "最多替换 3 件" in comparison["criteria"]["upgrade_scope"]
    assert "tier ≤ T3" in comparison["criteria"]["substitution_gate"]


def test_identical_plans_say_so_instead_of_faking_an_upgrade():
    plan_a = _plan("A", [("p1", 10.0, 3.0)])
    plan_b = {
        "plan_id": "B",
        "selection_mode": "substitution",
        "items": [{"id": "A-p1", "name": "p1", "price": 10.0, "match_score": 3.0}],
        "total": 10.0,
    }
    comparison = _compare_plans(plan_a, plan_b, budget_limit=None, max_upgrades=3)
    assert comparison["upgrades"] == []
    assert "件件相同" in comparison["recommendation"]
    assert comparison["shared_count"] == 1


def test_relevance_reselect_mode_is_disclosed_not_dressed_up_as_upgrade():
    """没有可辩护替换时，B 必须声明自己是「独立重排」。"""
    plan_a = _plan("A", [("p1", 10.0, 1.0), ("p2", 5.0, 1.0)])
    plan_b = _plan("B", [("x1", 30.0, 9.0), ("x2", 28.0, 8.0)], mode="relevance_reselect")
    comparison = _compare_plans(plan_a, plan_b, budget_limit=200.0, max_upgrades=3)
    rec = comparison["recommendation"]
    assert "没有做替换" in rec, rec
    assert "数据结论" in rec, rec
    assert comparison["criteria"]["selection_mode_b"] == "relevance_reselect"

