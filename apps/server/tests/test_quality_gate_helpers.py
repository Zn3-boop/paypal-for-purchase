"""quality_gate.py 的纯函数单元测试（不触网）—— 让质量门禁本身可回归。

quality_gate 的 `_prefilter_by_budget` / `_stats` 是纯函数，进 CI 无需 Channel3。
SCRIPTS_ROOT 让测试能从仓库根的 scripts/ 直接 import（pytest 收集时根目录不在
sys.path 上，需要显式插入）。

覆盖三件事（2026-11-13 审计修复）：
1. prefilter 必须剔除单价 > 预算的商品（模拟 planner 真实行为）；
2. mk_constraints 抽取团购句时，钢号要带材质上下文（316 → 316不锈钢）；
3. _stats 正确分桶 sat / unsat / unknown。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.constraints import Constraint  # noqa: E402
from scripts.quality_gate import _prefilter_by_budget, _stats, mk_constraints  # noqa: E402


def _budget(limit: float, scope: str = "per_item") -> Constraint:
    return Constraint(
        id="c-budget", type="budget", content=f"单件预算不超 {limit}",
        hard=True, source="demo", numeric_value=limit, status="active", scope=scope,
    )


class TestPrefilterByBudget:
    def test_drops_items_over_ceiling(self):
        items = [{"price": 10}, {"price": 45}, {"price": 90}]
        kept = _prefilter_by_budget(items, [_budget(45)])
        assert [i["price"] for i in kept] == [10, 45]

    def test_uses_min_of_multiple_budgets(self):
        items = [{"price": 10}, {"price": 45}, {"price": 90}]
        kept = _prefilter_by_budget(items, [_budget(90), _budget(45)])
        assert [i["price"] for i in kept] == [10, 45]

    def test_no_budget_keeps_all(self):
        items = [{"price": 10}, {"price": 90}]
        kept = _prefilter_by_budget(items, [])
        assert len(kept) == 2

    def test_budget_equal_to_price_kept(self):
        items = [{"price": 45}]
        kept = _prefilter_by_budget(items, [_budget(45)])
        assert len(kept) == 1


class TestMkConstraintsSpecContext:
    def test_steel_grade_has_material_context(self):
        cons = mk_constraints("我们团体采购一批保温杯，316不锈钢，500ml，单件预算不超 90。")
        contents = [c.content for c in cons]
        assert "316不锈钢" in contents, contents
        # 裸纯数字会被 satisfaction._keywords 的 isdigit() 过滤 → 永远「没有可比对的关键词」
        assert "316" not in contents, contents

    def test_capacity_normalized(self):
        cons = mk_constraints("我需要一支保温杯，500 ml 便携款。")
        assert any(c.content == "500ml" for c in cons)


class TestStatsBucketing:
    def test_counts_three_buckets(self):
        rows = [{"verdicts": [{"status": "sat"}, {"status": "unknown"}]},
                {"verdicts": [{"status": "unsat"}]}]
        assert _stats(rows) == {"sat": 1, "unsat": 1, "unknown": 1}

    def test_ignores_unknown_statuses(self):
        rows = [{"verdicts": [{"status": "sat"}, {"status": "n/a"}]}]
        assert _stats(rows) == {"sat": 1, "unsat": 0, "unknown": 0}