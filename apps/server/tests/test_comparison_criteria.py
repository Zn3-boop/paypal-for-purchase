"""A/B 对比标准的回归 —— 「凭什么这样比」必须是可核对的结构，不是文案。

契约（_compare_plans）：
  - criteria：同一候选池 + 两个目标函数（A 最低总价 / B 最高关键词匹配分）+ 同一可行性约束；
  - metrics：两方案的 total / relevance 总分 / 件数 / 共有件数，全部来自真实商品；
  - pareto：若一方在「总价」与「匹配分」上同时不劣，必须标记为被支配，
            否则标记为 trade_off（真实取舍）。不允许把「被支配」包装成「取舍」。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.planner import _compare_plans, _pareto_relation


def _plan(plan_id: str, items: list[tuple[str, float, float]]) -> dict:
    """items = [(name, price, match_score)]"""
    return {
        "plan_id": plan_id,
        "items": [
            {"id": f"{plan_id}-{name}", "name": name, "price": price, "match_score": score}
            for name, price, score in items
        ],
        "total": round(sum(price for _, price, _ in items), 2),
    }


def test_pareto_trade_off_when_a_cheaper_b_better_matched():
    plan_a = _plan("A", [("气球", 5.0, 1.0)])
    plan_b = _plan("B", [("横幅", 20.0, 9.0)])
    relation = _pareto_relation(plan_a, plan_b)
    assert relation["relation"] == "trade_off"
    assert relation["total_a"] == 5.0 and relation["total_b"] == 20.0
    assert relation["relevance_a"] == 1.0 and relation["relevance_b"] == 9.0


def test_pareto_marks_dominated_plan_instead_of_calling_it_trade_off():
    # A 又便宜、匹配分又高：B 被支配，必须点名，而不是说「各有取舍」
    plan_a = _plan("A", [("气球", 5.0, 9.0)])
    plan_b = _plan("B", [("横幅", 20.0, 1.0)])
    relation = _pareto_relation(plan_a, plan_b)
    assert relation["relation"] == "a_dominates"
    assert "方案 A" in relation["note"]


def test_compare_plans_exposes_criteria_and_metrics():
    plan_a = _plan("A", [("气球", 5.0, 1.0), ("彩带", 3.0, 0.5)])
    plan_b = _plan("B", [("气球", 6.0, 1.0), ("横幅", 20.0, 9.0)])
    comparison = _compare_plans(plan_a, plan_b, budget_limit=100.0)

    criteria = comparison["criteria"]
    assert criteria["domain"]
    assert "总价" in criteria["objective_a"]
    assert "关键词匹配" in criteria["objective_b"]
    # 证据必须说明匹配分只是词面重合，不含评分/评论
    assert "评分" in criteria["evidence"]

    metrics = comparison["metrics"]
    assert metrics["total_a"] == 8.0
    assert metrics["total_b"] == 26.0
    assert metrics["relevance_a"] == 1.5
    assert metrics["relevance_b"] == 10.0
    assert metrics["item_count_a"] == 2
    assert metrics["item_count_b"] == 2
    assert metrics["shared_count"] == comparison["shared_count"]

    assert comparison["pareto"]["relation"] in {"trade_off", "a_dominates", "b_dominates"}
    assert comparison["total_delta"] == 18.0


def test_compare_plans_blocks_claiming_quality_from_price():
    """match_score 缺失（本地目录）时不得伪造匹配分。"""
    plan_a = {"plan_id": "A", "items": [{"id": "x", "name": "x", "price": 2.0}], "total": 2.0}
    plan_b = {"plan_id": "B", "items": [{"id": "y", "name": "y", "price": 4.0}], "total": 4.0}
    comparison = _compare_plans(plan_a, plan_b, budget_limit=None)
    assert comparison["metrics"]["relevance_a"] == 0
    assert comparison["metrics"]["relevance_b"] == 0
    # 两方案匹配分都为 0、A 更便宜 → A 不劣，B 被支配
    assert comparison["pareto"]["relation"] == "a_dominates"
