"""规格 C：reason codes 回归 —— 纯函数、可复算、不伪造。

契约（改这些等于改产品规则，README 有对应判定式）：
  1) 优先级固定：FILLS_CATEGORY_GAP > CHEAPER_ALTERNATIVE > SAME_RELEASE_OTHER_EDITION
     > SAME_ARTIST > SAME_GENRE > HISTORY_DIRECTION；
  2) 一卡一个主码，至多一个辅码；
  3) 解析不出类型/品牌时绝不伪造 SAME_ARTIST / SAME_GENRE（降级到 HISTORY 或空码）；
  4) 纯函数：同输入同输出，不碰模型、不看随机数。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase import reason_codes as rc  # noqa: E402
from app.services.group_purchase.planner import _query_tokens  # noqa: E402

ANCHORS = _query_tokens("vinyl record rnb")


def _item(name: str, category: str, price: float, tags: list[str] | None = None) -> dict:
    return {"name": name, "category": category, "price": price, "tags": list(tags or [])}


PLAN = [_item("Alpha - 7-Inch Single", "Vinyl", 20.0, ["7-inch single", "Brand: Colemine"])]


def _code(**kwargs) -> dict:
    return rc.classify(**kwargs)


def test_fills_category_gap_beats_every_other_code():
    # 品类不在 A∪B 里（Vinyl Care vs Vinyl），但和检索词沾边（vinyl）
    cand = _item("Vinyl Record Cleaning Kit", "Vinyl Care", 14.5, ["vinyl"])
    out = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS, history_text="买一张 rnb 唱片")
    assert out["code"] == "FILLS_CATEGORY_GAP", out
    assert "品类" in out["text"]


def test_cheaper_alternative_beats_same_release():
    # tier=0（同一件）且更便宜 → 主码是 CHEAPER_ALTERNATIVE，不是 SAME_RELEASE
    cand = _item("Alpha - 7-Inch Single", "Vinyl", 12.0, ["7-inch single", "Brand: Colemine"])
    out = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS)
    assert out["code"] == "CHEAPER_ALTERNATIVE", out
    assert "$8.00" in out["text"], out["text"]


def test_same_release_other_edition_when_not_cheaper():
    cand = _item("Alpha - 12-Inch Single", "Vinyl", 22.0, ["12-inch single", "Brand: Colemine"])
    out = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS)
    assert out["code"] == "SAME_RELEASE_OTHER_EDITION", out
    assert "T1" in out["text"]


def test_same_artist_requires_a_parsed_brand():
    cand = _item("Omega - 7-Inch Single", "Vinyl", 24.0, ["7-inch single", "Brand: Colemine"])
    out = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS)
    assert out["code"] == "SAME_ARTIST", out


def test_same_genre_when_brand_differs():
    cand = _item("Omega - 7-Inch Single", "Vinyl", 24.0, ["7-inch single", "Brand: Numero"])
    out = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS)
    assert out["code"] == "SAME_GENRE", out


def test_unparsable_kind_never_fakes_same_genre():
    """只同 category、类型解析不出来 → tier 6，只能给 HISTORY_DIRECTION（不伪造同类型）。"""
    plan = [_item("Alpha - 7-Inch Single", "Music", 20.0, ["7-inch single"])]
    cand = _item("Rnb Cassette", "Music", 25.0, ["rnb"])
    out = _code(item=cand, plan_items=plan, anchor_tokens=ANCHORS, history_text="买一张 rnb")
    assert out["code"] == "HISTORY_DIRECTION", out
    assert "同方向" in out["text"]


def test_no_reason_when_below_relevance_floor():
    cand = _item("Garden Hose Nozzle", "Garden", 8.0, ["garden"])
    out = _code(item=cand, plan_items=[], anchor_tokens=ANCHORS)
    assert out["code"] == "", out
    assert out["rel"] == 0.0


def test_at_most_one_primary_and_one_secondary():
    cand = _item("Alpha - 12-Inch Single", "Vinyl", 12.0, ["12-inch single", "Brand: Colemine"])
    out = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS)
    assert out["code"] in rc.PRIORITY
    assert out["secondary"] is None or out["secondary"] in rc.PRIORITY
    # 主码与辅码不能重复
    assert out["secondary"] != out["code"]


def test_is_pure_function():
    cand = _item("Alpha - 12-Inch Single", "Vinyl", 22.0, ["12-inch single"])
    first = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS)
    second = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS)
    assert first == second


def test_rel_is_normalized_to_unit_interval():
    cand = _item("Vinyl Record Rnb", "Vinyl", 10.0, ["vinyl", "record", "rnb"])
    out = _code(item=cand, plan_items=PLAN, anchor_tokens=ANCHORS)
    assert 0.0 <= out["rel"] <= 1.0
    # 4 个锚点里命中 3 个 → 0.75
    assert out["rel"] == 0.75, out
