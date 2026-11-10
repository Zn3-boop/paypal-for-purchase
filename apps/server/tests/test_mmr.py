"""规格 B：MMR 回归 —— 取「最贴历史、又最不像方案里已有」的三件。

契约：
  - rel 必须先归一化（minmax），否则 λ 无意义；
  - relevance floor：rel < 0.25 的候选永不入选（MMR 只解决重复，不解决无关）；
  - sim 来自 tier（1 − tier/6）：已在方案里的那件 sim=1，会被压到底；
  - 一次最多 k 件；λ=1 退化为纯相关排序，λ=0 退化为纯多样。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.mmr import minmax, mmr_select  # noqa: E402


def _vinyl(name: str, fmt: str = "7-inch single") -> dict:
    return {"name": name, "category": "Vinyl", "price": 12.0, "tags": [fmt]}


PLAN_ITEM = _vinyl("Alpha - 7-Inch Single")


def test_minmax_normalizes_to_unit_interval():
    assert minmax([0, 5, 10]) == [0.0, 0.5, 1.0]
    assert minmax([]) == []
    # 全 0 → 全 0（没有相关度，不能凭空给 1）
    assert minmax([0.0, 0.0]) == [0.0, 0.0]
    # 全等正数 → 全 1（同一相关度，排序交给 sim 惩罚）
    assert minmax([3.0, 3.0]) == [1.0, 1.0]


def test_plan_duplicate_is_penalized_not_top_pick():
    """核心断言：rel 最高的那张如果是方案里已有的那件，MMR 不选它。"""
    same = _vinyl("Alpha - 7-Inch Single")            # tier 0 → sim 1.0
    other_edition = _vinyl("Alpha - 12-Inch Single", "12-inch single")  # tier 1
    different = _vinyl("Zeta Soul - 7-Inch Single")   # 同类型不同本体 → tier 4/5
    picked = mmr_select(
        [same, other_edition, different],
        [0.90, 0.85, 0.80],
        k=1,
        base_items=[PLAN_ITEM],
    )
    assert picked == [different], picked


def test_lambda_one_degrades_to_pure_relevance():
    same = _vinyl("Alpha - 7-Inch Single")
    different = _vinyl("Zeta Soul - 7-Inch Single")
    picked = mmr_select([same, different], [0.9, 0.8], k=1, lam=1.0, base_items=[PLAN_ITEM])
    assert picked == [same], picked


def test_lambda_zero_degrades_to_pure_diversity():
    same = _vinyl("Alpha - 7-Inch Single")
    different = _vinyl("Zeta Soul - 7-Inch Single")
    picked = mmr_select([same, different], [0.9, 0.8], k=1, lam=0.0, base_items=[PLAN_ITEM])
    assert picked == [different], picked


def test_relevance_floor_drops_irrelevant_candidates():
    irrelevant = _vinyl("Garden Hose Nozzle", "")
    picked = mmr_select([irrelevant], [0.10], k=3, base_items=[])
    assert picked == []


def test_selected_cards_penalize_each_other():
    """已经选进泳道的卡也进 S：两张一模一样的候选不会占满三个卡位。"""
    a = _vinyl("Zeta Soul - 7-Inch Single")
    b = _vinyl("Zeta Soul - 7-Inch Single")
    c = _vinyl("Omega - 7-Inch Single")
    picked = mmr_select([a, b, c], [1.0, 1.0, 0.9], k=2, base_items=[])
    assert len(picked) == 2
    assert c in picked, picked  # 第二张必须是不同的那件


def test_k_is_respected():
    cands = [_vinyl(f"Track {i} - 7-Inch Single") for i in range(5)]
    picked = mmr_select(cands, [1.0, 0.9, 0.8, 0.7, 0.6], k=3)
    assert len(picked) == 3
