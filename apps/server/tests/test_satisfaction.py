"""satisfaction.py 纯函数判定层 —— 「✅/❌/❔」必须是可复算的真话。

覆盖录制现场会当面问的三个问题：
1. 预算到底算没算对：$285 ≤ $350 → sat，证据串可直接读；
2. 「打不出 ✅ 的约束」怎么办：主观词（"要好一点"）→ unknown 而不是假装 ✅；
3. 「共 0 条」的根因还在不在：matched 是算出来的，不是写死的。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.satisfaction import (
    STATUS_SAT,
    STATUS_UNKNOWN,
    STATUS_UNSAT,
    evaluate,
    item_satisfies,
)


def _c(
    ctype: str,
    content: str,
    *,
    source: str | None = None,
    hard: bool = False,
    numeric_value: float | None = None,
    cid: str | None = None,
) -> Constraint:
    return Constraint(
        id=cid or f"c-{ctype}-{content}",
        type=ctype,
        content=content,
        source=source,
        hard=hard,
        numeric_value=numeric_value,
        status="active",
    )


def _item(item_id: str, name: str, price: float, tags: list[str] | None = None) -> dict:
    return {"id": item_id, "name": name, "category": "通用", "price": price, "tags": tags or []}


class TestBudget:
    def test_within_budget_sat_with_evidence(self):
        items = [_item("a", "水杯", 95), _item("b", "T恤", 98), _item("c", "帽子", 92)]
        res = evaluate([_c("budget", "总预算 350", hard=True, numeric_value=350)], items)
        assert res["passed"] == 1
        assert res["constraints"][0]["status"] == STATUS_SAT
        assert res["constraints"][0]["evidence"] == "$285.00 ≤ $350.00"

    def test_over_budget_unsat_with_shortfall(self):
        items = [_item("a", "水杯", 200), _item("b", "T恤", 193)]
        res = evaluate([_c("budget", "总预算 350", hard=True, numeric_value=350)], items)
        assert res["constraints"][0]["status"] == STATUS_UNSAT
        assert "超支 $43.00" in res["constraints"][0]["evidence"]

    def test_budget_is_combo_level_not_per_item(self):
        items = [_item("a", "水杯", 95)]
        res = evaluate([_c("budget", "总预算 100", hard=True, numeric_value=100)], items)
        assert res["items"][0]["results"][0]["status"] == "n/a"

    def test_budget_without_amount_is_unknown(self):
        res = evaluate([_c("budget", "预算别太贵")], [_item("a", "水杯", 95)])
        assert res["constraints"][0]["status"] == STATUS_UNKNOWN


class TestFunctional:
    def test_keyword_hit_sat(self):
        items = [_item("a", "无线耳机", 99, tags=["audio", "wireless"]), _item("b", "T恤", 20)]
        res = evaluate([_c("functional", "需要耳机")], items)
        assert res["constraints"][0]["status"] == STATUS_SAT
        assert "无线耳机" in res["constraints"][0]["evidence"]

    def test_keyword_miss_unsat(self):
        items = [_item("a", "T恤", 20)]
        res = evaluate([_c("functional", "需要耳机")], items)
        assert res["constraints"][0]["status"] == STATUS_UNSAT

    def test_item_level_pass_fail(self):
        items = [_item("a", "无线耳机", 99), _item("b", "T恤", 20)]
        res = evaluate([_c("functional", "需要耳机")], items)
        assert res["items"][0]["results"][0]["status"] == "pass"
        assert res["items"][1]["results"][0]["status"] == "fail"


class TestTaboo:
    def test_taboo_hit_unsat(self):
        items = [_item("a", "塑料杯", 30, tags=["plastic", "一次性"])]
        res = evaluate([_c("taboo", "不要塑料", source="成员 A")], items)
        assert res["constraints"][0]["status"] == STATUS_UNSAT
        assert res["constraints"][0]["source"] == "成员 A"
        assert "塑料杯" in res["constraints"][0]["evidence"]

    def test_taboo_clean_sat(self):
        items = [_item("a", "不锈钢杯", 89, tags=["stainless"])]
        res = evaluate([_c("taboo", "不要塑料")], items)
        assert res["constraints"][0]["status"] == STATUS_SAT


class TestEco:
    def test_eco_tag_hit_sat(self):
        items = [_item("a", "帆布袋", 25, tags=["recycled", "环保"])]
        res = evaluate([_c("preference", "要环保")], items)
        assert res["constraints"][0]["status"] == STATUS_SAT
        assert "标签命中环保" in res["constraints"][0]["evidence"]

    def test_eco_miss_unsat_not_unknown(self):
        # Channel3 没有 eco_label，但标签里能核对时，未命中就是未命中（不是撒谎的 ❔）。
        items = [_item("a", "普通T恤", 20, tags=["cotton"])]
        res = evaluate([_c("preference", "要环保")], items)
        assert res["constraints"][0]["status"] == STATUS_UNSAT


class TestUnknown:
    def test_subjective_quality_never_checkmarked(self):
        items = [_item("a", "无线耳机", 99)]
        res = evaluate([_c("preference", "要好一点")], items)
        assert res["constraints"][0]["status"] == STATUS_UNKNOWN
        assert res["constraints"][0]["evidence"] is None

    def test_subjective_goes_to_unknown_count_not_passed(self):
        items = [_item("a", "无线耳机", 99)]
        res = evaluate(
            [
                _c("preference", "要好一点"),
                _c("budget", "预算 300", hard=True, numeric_value=300),
            ],
            items,
        )
        assert res["passed"] == 1
        assert res["unknown"] == 1
        assert "满足 1/2 条" in res["headline"]
        assert "1 条无法判定" in res["headline"]


class TestAggregation:
    def test_headline_counts(self):
        items = [_item("a", "无线耳机", 95, tags=["recycled"])]
        constraints = [
            _c("budget", "预算 350", hard=True, numeric_value=350),
            _c("functional", "需要耳机"),
            _c("preference", "要环保"),
        ]
        res = evaluate(constraints, items)
        assert res["total"] == 3
        assert res["passed"] == 3
        assert res["unknown"] == 0
        assert "满足 3/3 条" in res["headline"]

    def test_max_two_failures_shown(self):
        items = [_item("a", "无线耳机", 99)]
        constraints = [
            _c("functional", "需要键盘"),
            _c("functional", "需要鼠标"),
            _c("taboo", "不要耳机"),
        ]
        res = evaluate(constraints, items)
        assert res["failed"] == 3
        assert res["max_failures_shown"] == 2
        shown = [c for c in res["constraints"] if c["visible"]]
        assert len(shown) == 2
        assert len(res["hidden_failures"]) == 1

    def test_no_constraints_not_judgeable(self):
        res = evaluate([], [_item("a", "T恤", 20)])
        assert res["judgeable"] is False
        assert res["headline"] == "还没有可判定的约束"

    def test_inactive_constraints_excluded(self):
        c = Constraint(
            id="c-old", type="functional", content="需要耳机",
            status="removed", source=None, hard=False,
        )
        res = evaluate([c], [_item("a", "T恤", 20)])
        assert res["judgeable"] is False


class TestItemSatisfies:
    def test_item_satisfies_positive(self):
        c = _c("functional", "需要耳机")
        assert item_satisfies(c, _item("a", "无线耳机", 99)) is True
        assert item_satisfies(c, _item("b", "T恤", 20)) is False
