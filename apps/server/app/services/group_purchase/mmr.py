"""MMR（Maximal Marginal Relevance）—— 推荐泳道的重排，一行公式。

Carbonell & Goldstein, SIGIR 1998:
    MMR = argmax_{c ∈ R\\S} [ λ·rel(c,q) − (1−λ)·max_{s∈S} sim(c,s) ]

在本项目里它不是「多样性技巧」，而是把两条轴写清楚：
    rel = 与本次检索锚点的相关度（必须归一化到 [0,1]，否则 λ 没有意义）
    sim = 与方案 A∪B / 已选卡片的相似度（由 substitution_tier 反推：1 − tier/6）
    S   = 方案 A ∪ 方案 B ∪ 已选中的推荐卡

三个必须守住的点：
  1) rel 先归一化（minmax），否则 λ 只是个拍脑袋的数；
  2) MMR 之前必须有 relevance floor：它只解决「重复」，不解决「无关」；
     没有下限会给出三件互不相同但全都无关的东西 —— 比重复更糟；
  3) sim 必须来自 tier：两张不同风格的黑胶标题里都有 vinyl/record，
     文本相似度会把它们判成高度相似，惩罚方向就反了。

一句实话：3 张卡时，MMR 的效果 ≈ 贪心去重。它的价值是可复算、可口播：
「推荐不是取分最高的三件，是取最贴你历史、又最不像方案里已有的三件。」
"""
from __future__ import annotations

from typing import Any, Callable

from app.services.group_purchase.tiering import similarity

DEFAULT_LAMBDA = 0.6
MIN_RELEVANCE = 0.25


def minmax(values: list[float]) -> list[float]:
    """线性归一化到 [0,1]；全等时：正数一律 1.0，全 0 一律 0.0。"""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [1.0 if v > 0 else 0.0 for v in values]
    return [round((v - lo) / (hi - lo), 6) for v in values]


def mmr_select(
    candidates: list[dict[str, Any]],
    rels: list[float],
    k: int = 3,
    lam: float = DEFAULT_LAMBDA,
    base_items: list[dict[str, Any]] | None = None,
    min_rel: float = MIN_RELEVANCE,
    sim_fn: Callable[[dict[str, Any], dict[str, Any]], float] | None = None,
) -> list[dict[str, Any]]:
    """贪心选 k 件：每步取 λ·rel − (1−λ)·max_sim 最大的候选。

    参数 rels 应是**归一化后的**打分尺度（见 minmax）；`min_rel` 作用在传进来的
    rels 上。调用方若已按「绝对相关度 ≥ 0.25」过滤过，应传 min_rel=0.0 ——
    否则 minmax 会让「只有两个候选」中的最小值变成 0 而被 floor 误杀。
    """
    sim = sim_fn or similarity
    base = [b for b in (base_items or []) if b]
    pool = [(c, float(r)) for c, r in zip(candidates, rels) if float(r) >= min_rel]
    picked: list[dict[str, Any]] = []
    while pool and len(picked) < max(0, int(k)):
        best_idx = 0
        best_score: float | None = None
        for idx, (cand, rel) in enumerate(pool):
            s_max = max((sim(cand, s) for s in base + picked), default=0.0)
            score = lam * rel - (1.0 - lam) * s_max
            if best_score is None or score > best_score + 1e-12:
                best_idx, best_score = idx, score
        cand, _rel = pool.pop(best_idx)
        picked.append(cand)
    return picked
