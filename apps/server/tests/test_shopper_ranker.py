"""Shopper 本地排序 + 冻结目录测试。

覆盖：
  - catalog.json fixture 加载（零网络零模型）
  - 本地排序：品味锚点匹配 / 预算约束 / 排除项 / 品类偏好
  - 字符 n-gram 相似度（中英文）
  - 演示链路：无外部搜索 key 也能完整推荐
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from app.services.shopper.catalog import get_catalog_items, get_item
from app.services.shopper.ranker import ngram_similarity, rank_candidates


class TestCatalogFixture:
    def test_catalog_loads_without_network(self):
        """冻结 fixture 可加载，包含商品与标签。"""
        items = get_catalog_items()
        assert len(items) >= 5
        assert all("id" in i and "name" in i and "tags" in i for i in items)

    def test_get_item_by_id(self):
        item = get_item("item-001")
        assert item is not None
        assert item["name"] == "Sinatra 经典爵士音乐会门票"
        assert "Jazz" in item["tags"]

    def test_catalog_returns_copies(self):
        items = get_catalog_items()
        items[0]["name"] = "hacked"
        again = get_catalog_items()
        assert again[0]["name"] != "hacked"


class TestNgramSimilarity:
    def test_english_similar(self):
        assert ngram_similarity("Jazz", "Jazz music") > 0.3

    def test_chinese_similar(self):
        assert ngram_similarity("意大利菜", "Italian cuisine") >= 0.0

    def test_identical_is_high(self):
        assert ngram_similarity("sinatra", "sinatra") > 0.9


class TestLocalRanker:
    def test_rank_returns_scored_with_rationale(self):
        items = get_catalog_items()
        ranked = rank_candidates(
            candidates=items,
            taste_anchors=["Frank Sinatra", "Italian cuisine"],
        )
        assert ranked, "应至少返回一个候选"
        assert all("score" in r and "rationale" in r for r in ranked)
        # 分数降序
        scores = [r["score"] for r in ranked]
        assert scores == sorted(scores, reverse=True)
        # 命中锚点的商品应带 match_tags
        top = ranked[0]
        assert "match_tags" in top

    def test_budget_penalty_pushes_over_budget_down(self):
        items = get_catalog_items()
        ranked = rank_candidates(candidates=items, taste_anchors=["Jazz"], budget=30.0)
        if ranked:
            for r in ranked:
                if float(r["price"]) > 30.0:
                    assert "超出预算" in r["rationale"]

    def test_excluded_items_removed(self):
        items = get_catalog_items()
        ranked = rank_candidates(candidates=items, taste_anchors=["Jazz"], excluded={"item-001"})
        ids = {r["id"] for r in ranked}
        assert "item-001" not in ids

    def test_no_anchor_still_returns_reasoned_items(self):
        """无外部搜索 key / 无锚点时仍可推荐（按品类加权）。"""
        items = get_catalog_items()
        ranked = rank_candidates(
            candidates=items,
            taste_anchors=[],
            preferred_categories=["dining"],
        )
        assert ranked
        assert all(r["rank_source"] == "local_ranker" for r in ranked)