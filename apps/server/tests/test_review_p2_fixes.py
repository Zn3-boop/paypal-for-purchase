"""P2 评审修复的回归测试（单品层语义不要总判定 / 数量词 cap / 品类硬过滤 / 软预算上限）。

这些用例保护的行为都是「演示现场会被问倒」的点：
- 用户说「一件大衣」时方案给 8 件 → 摧毁可信度；
- 布料/防尘布混进「帆布包」方案 → 一眼假；
- 「最贴题」无预算时退化成「最贵」→ $10,217 vs $1,733。
"""
from __future__ import annotations

import pytest

from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.planner import (
    _drop_raw_material_items,
    _parse_requested_quantity,
    _uniform_qty_hint,
    build_search_query,
    prefilter_by_constraints,
)


# ---------- 统一/同款意图的数量提示（2026-11-13 实测，团建统一队鞋）----------

def test_uniform_qty_hint_for_team_shoes():
    hint = _uniform_qty_hint(32, 14, "作为团建统一队鞋")
    assert "统一" in hint
    assert "32 件" in hint
    assert "同一 SKU" in hint
    assert "14 件为不同款式各 1 件" in hint
    assert "统一队" in hint


def test_uniform_qty_hint_empty_without_uniform_intent():
    assert _uniform_qty_hint(32, 14, "采购 32 双鞋") == ""


def _c(content: str, ctype: str = "functional", hard: bool = True, cid: str = "c1", numeric=None) -> Constraint:
    return Constraint(
        id=cid, type=ctype, content=content, hard=hard,
        source="agent_composer", numeric_value=numeric,
    )


# ---------- P0-2 数量词解析 ----------

@pytest.mark.parametrize("text,expected", [
    ("我需要一件大衣，中长款，红色", 1),
    ("我需要一个充电宝，轻薄便携", 1),
    ("买5个人团体的体育用品", 5),
    ("买三支笔", 3),
    ("ECV 社团采购", None),      # 没提数量 → 不 cap，交给 min/max_items
    ("买一些奖品", None),
])
def test_parse_requested_quantity(text, expected):
    assert _parse_requested_quantity(text) == expected


# ---------- P1-① 品类硬过滤 ----------

_FAKE_POOL = [
    {"id": "bag", "name": "Canvas Tote Bag", "category": "Tote Bags", "price": 18.0, "tags": ["canvas"]},
    {"id": "fabric", "name": "Cotton Duck Canvas Fabric", "category": "Fabric", "price": 6.99, "tags": ["by the yard"]},
    {"id": "drop", "name": "Heavy Duty Canvas Drop Cloth", "category": "Drop Cloths", "price": 19.98, "tags": []},
    {"id": "pack", "name": "Canvas Backpack", "category": "Backpacks", "price": 29.0, "tags": ["canvas"]},
]


def test_prefilter_drops_raw_material_for_finished_product():
    """「帆布包」需求：布料/防尘布即使命中 canvas 也不能进方案。"""
    kept = prefilter_by_constraints(_FAKE_POOL, [_c("帆布包")], semantic_pool=True)
    ids = {it["id"] for it in kept}
    assert ids == {"bag", "pack"}, f"材料/半成品混进成品需求：{ids}"


def test_prefilter_keeps_material_when_material_is_requested():
    """「买帆布面料」是材料需求，不能把布料商品误杀。"""
    kept = _drop_raw_material_items(_FAKE_POOL, [_c("帆布面料")])
    assert {it["id"] for it in kept} == {"bag", "fabric", "drop", "pack"}


def test_prefilter_never_empties_pool_of_only_material():
    """全池都是材料商品时不下杀手（否则会把「无解」造出来）。"""
    only_material = [it for it in _FAKE_POOL if it["id"] in {"fabric", "drop"}]
    kept = _drop_raw_material_items(only_material, [_c("帆布包")])
    assert len(kept) == 2


# ---------- 充电宝：检索词同义映射（中文原句 → Channel3 英文检索词） ----------

def test_build_search_query_maps_charging_bank():
    query, display, anchors = build_search_query("我需要一个充电宝，轻薄便携，白色，小容量")
    assert "power bank" in query, "充电宝必须被映射成 power bank，否则 Channel3 召回 0 → 假无解"
    assert "portable" in query
    assert display == "我需要一个充电宝，轻薄便携，白色，小容量"
    assert "power bank" in anchors


def test_synonym_covers_charging_bank():
    from app.services.group_purchase.satisfaction import SYNONYM_KEYWORDS
    variants = SYNONYM_KEYWORDS.get("充电宝") or ()
    assert "power bank" in variants
    assert "powerbank" in variants
