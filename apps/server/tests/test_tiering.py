"""替代分级的回归 —— 判定必须**跨品类**成立，不能只为黑胶写死。

四个品类各一组：黑胶（长度/转速）、桌游（人数/适龄）、水瓶（容量）、球类（尺寸）。
每组的期望都来自同一套通用阶梯，而不是品类特判。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.tiering import (  # noqa: E402
    MAX_PRICE_UPLIFT,
    MAX_UPGRADE_TIER,
    NEVER_TIER,
    price_uplift_ok,
    similarity,
    substitution_tier,
    tier_label,
)


def _vinyl(name: str, tags: list[str], price: float = 12.0) -> dict:
    return {"id": name[:12], "name": name, "category": "Vinyl", "tags": tags, "price": price}


# ------------------------------------------------------------------ 黑胶
def test_same_release_same_format_is_t0():
    base = _vinyl("Winston Theme - 7-Inch Single", ["Funk", "7-inch single"])
    cand = _vinyl("Winston Theme - 7-Inch Single (Repress)", ["Funk", "7-inch single"])
    tier, basis = substitution_tier(cand, base)
    assert tier == 0, basis


def test_same_release_other_format_is_t1():
    base = _vinyl("Winston Theme - 7-Inch Single", ["7-inch single"])
    cand = _vinyl("Winston Theme - 12-Inch Single", ["12-inch single"])
    tier, basis = substitution_tier(cand, base)
    assert tier == 1, basis


def test_same_kind_same_spec_other_artist_is_t4():
    """不同艺人、同类型同规格：可以解释，但不能用于 B 的替换（> MAX_UPGRADE_TIER 之外才拦）。"""
    base = _vinyl("Winston Theme - 7-Inch Single", ["Funk", "7-inch single"])
    cand = _vinyl("Don't Down Me People - 7-Inch Single", ["Soul", "7-inch single"])
    tier, basis = substitution_tier(cand, base)
    assert tier == 4, basis
    assert tier > MAX_UPGRADE_TIER  # T4 不得用于替换


def test_same_kind_unknown_spec_is_t5():
    base = _vinyl("Ikebe Shakedown - Unqualified", ["Artist: Ikebe Shakedown", "Format: Vinyl 7 inch single"])
    cand = _vinyl("Rnb Vol. 1", ["World Wide Stereo"])
    tier, basis = substitution_tier(cand, base)
    assert tier == 5, basis
    assert tier > MAX_UPGRADE_TIER


def test_different_category_is_never_usable():
    base = _vinyl("Winston Theme - 7-Inch Single", ["7-inch single"])
    cand = {"id": "b1", "name": "Insulated Water Bottle 500ml", "category": "Kitchen", "tags": [], "price": 20}
    tier, _ = substitution_tier(cand, base)
    assert tier == NEVER_TIER
    assert similarity(cand, base) == 0.0


# ------------------------------------------------------------------ 桌游
def test_board_game_same_kind_is_t5_not_t6():
    """「环保桌游·森林棋」vs「环保桌游·生态大富翁」：同类型但主体不同 → T5，不是 T6。"""
    base = {"id": "g1", "name": "环保桌游·森林棋", "category": "桌游", "tags": ["桌游", "环保"], "price": 25}
    cand = {"id": "g2", "name": "环保桌游·生态大富翁", "category": "桌游", "tags": ["桌游", "环保"], "price": 30}
    tier, basis = substitution_tier(cand, base)
    assert tier == 5, basis


def test_board_game_and_bottle_are_incomparable():
    base = {"id": "g1", "name": "环保桌游·森林棋", "category": "桌游", "tags": ["桌游"], "price": 25}
    cand = {"id": "w1", "name": "不锈钢保温瓶 500ml", "category": "水杯", "tags": ["保温瓶"], "price": 18}
    tier, _ = substitution_tier(cand, base)
    assert tier == NEVER_TIER


# ------------------------------------------------------------------ 水瓶 / 球类
def test_bottle_same_capacity_other_brand_is_t4():
    base = {"id": "w1", "name": "Water Bottle 500ml", "category": "Kitchen", "tags": [], "price": 12}
    cand = {"id": "w2", "name": "Insulated Water Bottle 500ml", "category": "Kitchen", "tags": [], "price": 16}
    tier, basis = substitution_tier(cand, base)
    assert tier == 4, basis


def test_ball_same_kind_same_size_is_t4_and_differs_by_spec_at_t5():
    base = {"id": "b1", "name": "Soccer Ball Size 5", "category": "Sports", "tags": ["ball"], "price": 30}
    same = {"id": "b2", "name": "Training Soccer Ball Size 5", "category": "Sports", "tags": ["ball"], "price": 42}
    other = {"id": "b3", "name": "Training Soccer Ball Size 4", "category": "Sports", "tags": ["ball"], "price": 28}
    assert substitution_tier(same, base)[0] == 4, substitution_tier(same, base)[1]
    assert substitution_tier(other, base)[0] == 5, substitution_tier(other, base)[1]


# ------------------------------------------------------------------ 价格硬门 / 工具
def test_price_uplift_cap_is_enforced():
    assert price_uplift_ok(150.0, 100.0) is True
    assert price_uplift_ok(100.0 * (1 + MAX_PRICE_UPLIFT) + 0.01, 100.0) is False
    # 更便宜永远允许
    assert price_uplift_ok(60.0, 100.0) is True


def test_identical_items_have_full_similarity_and_labels_are_stable():
    a = _vinyl("Winston Theme - 7-Inch Single", ["Funk", "7-inch single"])
    assert similarity(a, a) == 1.0
    for tier in range(NEVER_TIER + 1):
        assert tier_label(tier) and tier_label(tier) != "未知"
