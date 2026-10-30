"""本地排序器 —— 外部搜索失效时稳定兜底。

设计目标（解耦三步）：
  1. Channel3 是可选增强层，核心链路：
     LLM 理解意图 -> 候选商品 -> 本地排序 -> Orders v2 结账
  2. 演示数据冻结成本地 fixture（catalog.json），演示/录视频零外部调用。
  3. 排序器零模型文件：规则 + 字符 n-gram 相似度（几十行，无 embedding）。

ranking pipeline：
  score(item, profile) =
    taste_overlap      (标签与用户品味锚点的 n-gram 相似度加权)
    + category_weight  (用户画像偏好的品类加权)
    - constraint_penalty (健康/预算约束不符则重罚)

返回的每个候选附带 rationale（可解释性面板数据源），
保证"为什么推荐这个"永远有本地可解释依据。
"""
from __future__ import annotations

import logging
import math
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# 字符 n-gram 相似度（无模型、零依赖、中英文都适用）
# ============================================================

def _char_ngrams(text: str, n: int = 2) -> set[str]:
    """字符级 2-gram。比单词 bag-of-words 对中文/拼写差异更鲁棒。"""
    text = re.sub(r"\s+", "", (text or "").lower())
    if len(text) < n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def ngram_similarity(a: str, b: str, n: int = 2) -> float:
    """Jaccard 字符 2-gram 相似度 [0,1]。"""
    ga, gb = _char_ngrams(a, n), _char_ngrams(b, n)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return inter / (len(ga) + len(gb) - inter) if (len(ga) + len(gb) - inter) else 0.0


# ============================================================
# 排序器
# ============================================================

CATEGORY_WEIGHTS = {
    "music": 1.0,
    "dining": 1.0,
    "film": 0.9,
    "book": 0.8,
    "donation": 0.7,
    "experience": 0.9,
    "merch": 0.6,
}

MAX_SCORE = 10.0


def rank_candidates(
    *,
    candidates: list[dict[str, Any]],
    taste_anchors: list[str],
    preferred_categories: list[str] | None = None,
    budget: float | None = None,
    excluded: set[str] | None = None,
) -> list[dict[str, Any]]:
    """本地排序：对候选商品按品味匹配度降序，附加 rationale。

    返回按 score 降序的候选，每项附带:
      - score:        [0, MAX_SCORE]
      - match_tags:   命中的标签列表
      - rationale:    人类可读的可解释理由（支付可解释性面板用）
    """
    excluded = excluded or set()
    preferred_categories = preferred_categories or []
    scored: list[dict[str, Any]] = []

    for item in candidates:
        item_id = str(item.get("id", ""))
        if item_id in excluded:
            continue

        # 1) 品味锚点匹配：商品 tags 与用户锚点的 n-gram 相似度
        tags = item.get("tags", [])
        name = item.get("name", "")
        best_overlap = 0.0
        matched_tags: list[str] = []
        for anchor in taste_anchors:
            best = 0.0
            for tag in tags:
                sim = max(ngram_similarity(anchor, tag), ngram_similarity(anchor, name))
                best = max(best, sim)
            if best > 0.35:
                matched_tags.append(anchor)
            best_overlap = max(best_overlap, best)

        # 2) 品类偏好加权
        cat = str(item.get("category", ""))
        cat_weight = CATEGORY_WEIGHTS.get(cat, 0.5)
        if preferred_categories and cat in preferred_categories:
            cat_weight += 0.5

        # 3) 预算约束（不符则重罚）
        price = float(item.get("price", 0) or 0)
        constraint_penalty = 0.0
        if budget and price > budget:
            constraint_penalty = 4.0

        # 总分 = 匹配度(0-6) + 品类加权(0-3) - 约束罚分
        score = (
            min(best_overlap * 6.0, 6.0)
            + min(cat_weight, 3.0)
            - constraint_penalty
        )
        score = max(0.0, min(score, MAX_SCORE))

        if score <= 0 and not matched_tags:
            continue

        rationale = _build_rationale(
            item, taste_anchors, matched_tags, best_overlap, budget, price
        )
        scored.append(
            {
                **item,
                "score": round(score, 3),
                "match_tags": matched_tags,
                "rationale": rationale,
                "rank_source": "local_ranker",
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def _build_rationale(
    item: dict[str, Any],
    anchors: list[str],
    matched: list[str],
    best_overlap: float,
    budget: float | None,
    price: float,
) -> str:
    parts: list[str] = []
    if matched:
        parts.append("契合你的品味锚点：" + "、".join(matched))
    elif anchors:
        parts.append("与你的偏好相关度较高")
    parts.append(f"属于{item.get('category', '其他')}品类")
    if budget and price <= budget:
        parts.append(f"价格 {price:.2f} 在你的预算内")
    if budget and price > budget:
        parts.append(f"价格 {price:.2f} 超出预算 {budget:.2f}，请谨慎")
    return "；".join(parts) if parts else f"为你推荐{item.get('name', '')}"