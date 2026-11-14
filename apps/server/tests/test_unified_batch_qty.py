"""同款×N 批量订购（2026-11-14）：统一/同款/队 + 明确数量 → 1 个主款 SKU × N。

保护的评审点（/review 追问「32 双统一队鞋」）：
- 「32 双统一队鞋」→ 方案只有 1 个 SKU × 32，而不是 32 个不同 SKU 各 1 件；
- 「12 把键盘」无统一词 → 维持多 SKU 各 1 件，不误伤普通批量；
- 「5 人份统一奖品」是软数量 → 不触发同款×N（不同款也能满足）；
- B 方案在主款同族内做可辩护升级（匹配分严格更高 + T0-T3 + 涨幅受控），否则同款同量；
- 预算内装不下 N 件 → 空方案 fallback（不静默减量、不硬塞超预算）。
"""
from __future__ import annotations

import pytest

from app.services.group_purchase import planner
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.planner import (
    _generate_trade_offs,
    _group_unified_qty,
    _parse_requested_quantity,
    _pick_unified_main,
    _unified_batch_qty,
    build_search_query,
    generate_dual_plans,
)


# ---------- 触发判定：_unified_batch_qty ----------

@pytest.mark.parametrize("text,expected", [
    ("32 双统一队鞋", 32),
    ("统一购买 10 件队服", 10),
    ("同款训练鞋买 15 套", 15),
    ("", None),
    (None, None),
])
def test_unified_batch_qty_triggers(text, expected):
    assert _unified_batch_qty(text) == expected


@pytest.mark.parametrize("text", [
    "12 把键盘",            # 无统一词 → 普通多 SKU（回归）
    "5 人份统一环保桌游",    # 人份软数量 → 不触发
    "团建统一配备",          # 有统一词但没明确数量
    "采购 32 双鞋",          # 无统一词
])
def test_unified_batch_qty_not_triggered(text):
    assert _unified_batch_qty(text) is None


# ---------- 主款选择：_pick_unified_main ----------

_NIKE_SHOES = [
    {"id": "shoe_c", "name": "Basic Team Shoe", "category": "Shoes", "price": 10.0,
     "tags": ["brand: Nike", "鞋"], "source": "k1_catalog"},
    {"id": "shoe_a", "name": "Comfort Team Shoe", "category": "Shoes", "price": 14.0,
     "tags": ["brand: Nike", "鞋", "舒适"], "source": "k1_catalog"},
    {"id": "shoe_b", "name": "Pro Team Shoe", "category": "Shoes", "price": 26.0,
     "tags": ["brand: Nike", "鞋", "专业", "旗舰"], "source": "k1_catalog"},
]
_SCORES = {
    "shoe_c": {"score": 1.0, "match_tags": []},
    "shoe_a": {"score": 3.0, "match_tags": []},
    "shoe_b": {"score": 6.0, "match_tags": []},
}
_BASE = {"id": "shoe_c", "name": "Basic Team Shoe", "category": "Shoes", "price": 10.0,
         "tags": ["brand: Nike"]}


def test_unified_main_cheap_picks_lowest_price():
    main = _pick_unified_main(
        candidates=_NIKE_SHOES, strategy="cheap", relevance=_SCORES,
        budget_limit=None, unified_qty=32,
    )
    assert main is not None
    assert main["id"] == "shoe_c"


def test_unified_main_b_upgrades_within_family():
    """B：同族（同 category）内、匹配分严格更高、涨幅受控的替代款。

    shoe_b 分最高但 26/10 = 160% > 50% 被拒；shoe_a 14/10 = 40% 且同品牌同类型
    （T3）→ 成为 B 主款。"""
    main = _pick_unified_main(
        candidates=_NIKE_SHOES, strategy="relevance", relevance=_SCORES,
        budget_limit=None, unified_qty=32, base_item=_BASE,
    )
    assert main is not None
    assert main["id"] == "shoe_a"
    assert main["_substitution_tier"] == 3


# ---------- P0 修复：数量词表补「盏」（2026-11-14） ----------

@pytest.mark.parametrize("text,expected", [
    ("我需要一盏桌面台灯，全光谱护眼，银灰色，可触控调光款", 1),
    ("我要三盏台灯", 3),
    ("买 5 盏感应夜灯", 5),
])
def test_parse_requested_quantity_lamp_unit(text, expected):
    """「盏」必须识别：否则「一盏台灯」被当成没数量 → 默认出 8 件（P0 实测 $1726）。"""
    assert _parse_requested_quantity(text) == expected


# ---------- P1 修复：规格数字(20W/10000mAh)进检索池 + 快充映射（2026-11-14） ----------

def test_build_search_query_powerbank_specs():
    """「20W 快充 10000mAh」必须进检索词：否则 Channel3 召回只有 2 件（实测）。"""
    query, _, _ = build_search_query(
        "我们团体采购一批便携充电宝，20W 快充，深空灰，10000mAh，单件预算不超 90，一共采购 22 个"
    )
    assert "20w" in query
    assert "10000mah" in query
    assert "fast charging" in query
    assert "power bank" in query


def test_build_search_query_no_spec_noise():
    """普通数字（22 个/90 预算）不带单位后缀 → 不混进英文检索词。"""
    query, _, _ = build_search_query("我们团体采购 22 个充电宝，单件预算不超 90")
    english_part = query.split(" | ")[0].lower()
    assert "power bank" in english_part
    assert not any(t in english_part for t in ("22w", "22mah", "22个", "90w"))


# ---------- P1-① 检索词污染修复：杯具语境「水杯」→ cup 而非 water bottle ----------

def test_build_search_query_mug_context_no_water_bottle():
    """「陶瓷马克杯…工作室日常水杯」→ 检索词含 mug/cup/ceramic，绝不含 water bottle。"""
    query, _, anchors = build_search_query(
        "我们团体采购一批陶瓷马克杯，加厚陶瓷，米白色，带杯盖，单件预算不超 28，一共采购 40 只，工作室日常水杯"
    )
    assert "mug" in query
    assert "cup" in query
    assert "ceramic" in query
    assert "water bottle" not in query


def test_build_search_query_water_bottle_kept_without_cup_context():
    """无杯具语境时「水瓶」仍映射 water bottle（不误伤真实水瓶需求）。"""
    query, _, _ = build_search_query("买一个不锈钢水瓶，500ml")
    assert "water bottle" in query


# ---------- P1-② 团购弱信号默认同款×N ----------

def test_group_unified_qty_triggers():
    assert _group_unified_qty("我们团体采购一批陶瓷马克杯，一共 40 只") == 40
    assert _group_unified_qty("团购 20 件文化衫") == 20


def test_group_unified_qty_not_triggered():
    assert _group_unified_qty("12 把键盘") is None                          # 无团购词
    assert _group_unified_qty("团购 20 件不同款式 T 恤") is None            # 反意词「不同款式」
    assert _group_unified_qty("团购 5 人份奖品") is None                     # 软数量
    assert _group_unified_qty("团购一件大衣") is None                        # N=1 不适用
    assert _group_unified_qty("") is None


def test_group_unified_qty_variety_words_gate():
    """明确多样式（各一款/多款…）→ 绝不默认同款。"""
    for t in ("团购 20 件各一款的杯子", "一批 10 款不同样式的笔"):
        assert _group_unified_qty(t) is None


# ---------- P3-⑤ 病句：1 件「取自」候选里最便宜的 ----------

def test_single_item_tradeoff_phrase():
    """count==1 时「1 件取自候选里最便宜的」，不是「1 件都取」（病句）。"""
    trades = _generate_trade_offs("低价优先", [{"price": 28.0}], 28.0, None, [], "测试")
    assert any("1 件取自候选里最便宜的" in t for t in trades)
    assert not any("1 件都取" in t for t in trades)


def test_multi_item_tradeoff_phrase_unchanged():
    """count>1 时保持「N 件都取候选里最便宜的」。"""
    trades = _generate_trade_offs(
        "低价优先",
        [{"price": 5.5}, {"price": 6.5}], 12.0, None, [], "测试",
    )
    assert any("2 件都取候选里最便宜的" in t for t in trades)


# ---------- 集成：团购弱信号 ----------

@pytest.mark.asyncio
async def test_integration_group_40_mugs_single_sku_x40(monkeypatch):
    """「团体采购 40 只马克杯」→ 默认同款×40（1 个 SKU），不再拆成不同款。"""
    mugs = [
        {"id": f"mug_{i}", "name": f"Ceramic Mug {i}", "category": "Mugs",
         "price": 5.5 + i, "tags": ["马克杯", "陶瓷"], "source": "k1_catalog"}
        for i in range(5)
    ]
    fake = await _fake_retrieve(mugs)
    monkeypatch.setattr(planner, "_retrieve_catalog", fake)

    result = await generate_dual_plans(
        "我们团体采购一批陶瓷马克杯，一共 40 只，工作室日常水杯", constraints=[], use_channel3=True,
    )
    assert result["feasible"] is True
    pa = result["plan_a"]
    assert pa["selection_mode"] == "unified_xN"
    assert len(pa["items"]) == 1
    assert pa["items"][0]["quantity"] == 40
    assert pa["total"] == pytest.approx(pa["items"][0]["price"] * 40)


@pytest.mark.asyncio
async def test_integration_group_unified_budget_tight_fallback(monkeypatch):
    """团购弱信号 + 预算内单款装不下 → 退化为同族多 SKU 兜底（不直接无解）。"""
    mugs = [
        {"id": f"mug_{i}", "name": f"Ceramic Mug {i}", "category": "Mugs",
         "price": 5.5 + i, "tags": ["马克杯", "陶瓷"], "source": "k1_catalog"}
        for i in range(5)
    ]
    fake = await _fake_retrieve(mugs)
    monkeypatch.setattr(planner, "_retrieve_catalog", fake)

    tight = [Constraint(
        id="b1", type="budget", content="预算不超过 20", hard=True,
        source="member_A", numeric_value=20.0,
    )]
    result = await generate_dual_plans(
        "我们团体采购一批陶瓷马克杯，一共 40 只，工作室日常水杯", constraints=tight, use_channel3=True,
    )
    # 40 × 5.5 = 220 > 20 → 单款装不下；弱信号退回多 SKU，仍有方案但件数不足如实标注
    assert result["plan_a"] is not None
    assert result["plan_a"]["selection_mode"] == "unified_xN_fallback"
    assert result["plan_a"]["total"] <= 20.01
    assert sum(it["quantity"] for it in result["plan_a"]["items"]) < 40


def test_unified_main_b_same_score_keeps_base():
    """同分不升级：多花的钱买不到额外关键词证据 → 与 A 同款同量。"""
    same = {k: {"score": 3.0, "match_tags": []} for k in _SCORES}
    main = _pick_unified_main(
        candidates=_NIKE_SHOES, strategy="relevance", relevance=same,
        budget_limit=None, unified_qty=32, base_item=_BASE,
    )
    assert main is not None
    assert main["id"] == "shoe_c"


def test_unified_main_budget_too_tight_returns_none():
    """预算内装不下 N 件 → None（上层走空方案 fallback，不静默减量）。"""
    main = _pick_unified_main(
        candidates=_NIKE_SHOES, strategy="cheap", relevance=_SCORES,
        budget_limit=300.0, unified_qty=32,  # 最便宜也 10×32=320 > 300
    )
    assert main is None


# ---------- 集成：monkeypatch _retrieve_catalog ----------

async def _fake_retrieve(items):
    async def _fake(query, use_channel3):
        # 7 元组（P2-④ 起 _retrieve_catalog 契约）：候选数 = 本地目录条数
        return (list(items), "k1_catalog", query, "", [], "disabled", len(items))
    return _fake


@pytest.mark.asyncio
async def test_integration_32_unified_shoes_single_sku_x32(monkeypatch):
    """「32 双统一队鞋」→ A/B 各 1 个 SKU × 32，total = price × 32。"""
    fake = await _fake_retrieve(_NIKE_SHOES)
    monkeypatch.setattr(planner, "_retrieve_catalog", fake)

    result = await generate_dual_plans("32 双统一队鞋", constraints=[], use_channel3=True)
    assert result["feasible"] is True

    pa, pb = result["plan_a"], result["plan_b"]
    assert pa["selection_mode"] == "unified_xN"
    assert len(pa["items"]) == 1
    assert pa["items"][0]["quantity"] == 32
    assert pa["total"] == pytest.approx(pa["items"][0]["price"] * 32)

    assert len(pb["items"]) == 1
    assert pb["items"][0]["quantity"] == 32
    assert pb["total"] == pytest.approx(pb["items"][0]["price"] * 32)


@pytest.mark.asyncio
async def test_integration_12_keyboards_still_distinct_skus(monkeypatch):
    """「12 把键盘」无统一词 → 回归：12 个不同 SKU 各 1 件。"""
    keyboards = [
        {"id": f"kb_{i}", "name": f"Mechanical Keyboard {i}", "category": "Keyboards",
         "price": 10.0 + i, "tags": ["键盘", "机械"], "source": "k1_catalog"}
        for i in range(12)
    ]
    fake = await _fake_retrieve(keyboards)
    monkeypatch.setattr(planner, "_retrieve_catalog", fake)

    result = await generate_dual_plans("12 把键盘", constraints=[], use_channel3=True)
    assert result["feasible"] is True

    pa = result["plan_a"]
    assert len(pa["items"]) == 12
    assert {it["quantity"] for it in pa["items"]} == {1}


@pytest.mark.asyncio
async def test_integration_headcount_uniform_not_triggered(monkeypatch):
    """「5 人份统一奖品」软数量 → 不触发同款×N（仍多 SKU 各 1 件）。"""
    pool = [
        {"id": f"gift_{i}", "name": f"Team Gift {i}", "category": "Gifts",
         "price": 5.0 + i, "tags": ["奖品", "团队"], "source": "k1_catalog"}
        for i in range(8)
    ]
    fake = await _fake_retrieve(pool)
    monkeypatch.setattr(planner, "_retrieve_catalog", fake)

    result = await generate_dual_plans("5 人份统一奖品", constraints=[], use_channel3=True)
    assert result["feasible"] is True

    pa = result["plan_a"]
    assert pa["selection_mode"] != "unified_xN"
    assert {it["quantity"] for it in pa["items"]} == {1}


@pytest.mark.asyncio
async def test_integration_budget_too_tight_fallback(monkeypatch):
    """预算内装不下 32 件 → 空方案 fallback（feasible=False，plan 置 None）。"""
    fake = await _fake_retrieve(_NIKE_SHOES)
    monkeypatch.setattr(planner, "_retrieve_catalog", fake)

    tight = [Constraint(
        id="b1", type="budget", content="预算不超过 100", hard=True,
        source="member_A", numeric_value=100.0,
    )]
    result = await generate_dual_plans("32 双统一队鞋", constraints=tight, use_channel3=True)
    assert result["feasible"] is False
    assert result["plan_a"] is None
    assert result["plan_b"] is None
    assert result["fallback"]