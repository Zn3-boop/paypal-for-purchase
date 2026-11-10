"""推荐原因码（reason codes）—— 一张卡一个主码，判定全是纯函数。

为什么需要它：三段式出处（你搜过 X → 本次检索 Y → 命中 N 件）证明「这张卡从哪来」，
不解释「为什么值得看」。原因码补后半句，并且必须**可复算**：同输入同输出、不碰模型、
每个码在 README 里有一行判定式。

六个码（优先级从高到低，命中即停）：
  1 FILLS_CATEGORY_GAP          品类不在 A∪B 里，且 rel ≥ 0.25
  2 CHEAPER_ALTERNATIVE         与方案里某件 tier ≤ T4，且比它便宜
  3 SAME_RELEASE_OTHER_EDITION  与方案里某件 tier == T1（同一件、另一个版本）
  4 SAME_ARTIST                 与方案里某件 tier ∈ {T2, T3}（同品牌/同艺人）
  5 SAME_GENRE                  与方案里某件 tier ∈ {T4, T5}（同一类型）
  6 HISTORY_DIRECTION           以上都不成立，但 rel ≥ 0.25

三条规矩（改这里等于改产品规则）：
  1) 一张卡一个主码，至多一个辅码（辅码只做补充，不改主叙事）；
  2) 每个码都是纯函数：tier 解析不出来就是 T6，**绝不伪造** SAME_ARTIST / SAME_GENRE；
  3) rel 归一化到 [0,1]：rel = min(1, 命中锚点数 / 4)，与 planner 的词干规则一致。

一条必须接受的事实：按这个优先级，多数卡的主码是 HISTORY_DIRECTION / SAME_GENRE，
而不是 SAME_RELEASE_OTHER_EDITION —— 推荐的本质是「同方向」，不是「更好」。
承认这一点，UI 就不该暗示「这张更值得买」。
"""
from __future__ import annotations

from typing import Any

from app.services.group_purchase.planner import _query_tokens
from app.services.group_purchase.tiering import substitution_tier

# rel 的归一化分母：命中 4 个锚点 token 即视为完全相关（≥1 个即过 0.25 下限）。
REL_TOKEN_CAP = 4
MIN_RELEVANCE = 0.25

PRIORITY: tuple[str, ...] = (
    "FILLS_CATEGORY_GAP",
    "CHEAPER_ALTERNATIVE",
    "SAME_RELEASE_OTHER_EDITION",
    "SAME_ARTIST",
    "SAME_GENRE",
    "HISTORY_DIRECTION",
)


def _as_dict(obj: Any) -> dict[str, Any]:
    """PlanItem / ShoppingPlan / dict 统一成 dict（reason code 要能对方案件判定）。"""
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {
        "name": getattr(obj, "name", ""),
        "category": getattr(obj, "category", ""),
        "price": getattr(obj, "price", 0),
        "tags": list(getattr(obj, "tags", []) or []),
    }


def _item_text(item: dict[str, Any]) -> str:
    parts = [str(item.get(k) or "") for k in ("name", "title", "category", "description")]
    tags = item.get("tags") or []
    if isinstance(tags, (list, tuple)):
        parts.append(" ".join(str(t) for t in tags))
    elif tags:
        parts.append(str(tags))
    return " ".join(p for p in parts if p)


def _hits(tokens: set[str], anchors: set[str]) -> int:
    """命中数：完全相等，或英文词干级命中（规则与 planner._is_relevant_item 一致）。"""
    n = 0
    for t in tokens:
        if t in anchors:
            n += 1
            continue
        if t.isascii() and len(t) >= 4:
            if any(
                a.isascii() and len(a) >= 4 and (t.startswith(a) or a.startswith(t))
                for a in anchors
            ):
                n += 1
    return n


def relevance(item: Any, anchor_tokens: set[str]) -> float:
    """与本次检索锚点的相关度 ∈ [0,1]；无锚点时一律 0（宁可不给推荐）。"""
    if not anchor_tokens:
        return 0.0
    tokens = _query_tokens(_item_text(_as_dict(item)))
    if not tokens:
        return 0.0
    return round(min(1.0, _hits(tokens, anchor_tokens) / REL_TOKEN_CAP), 4)


def _name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("title") or "").strip()


def _price(item: dict[str, Any]) -> float:
    try:
        return float(item.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _category(item: dict[str, Any]) -> str:
    return str(item.get("category") or "").strip().lower()


def _text_for(code: str, other: dict[str, Any], item: dict[str, Any], history_text: str) -> str:
    name = _name(other)
    if code == "FILLS_CATEGORY_GAP":
        return f"方案里没有「{str(item.get('category') or '').strip()}」这个品类"
    if code == "CHEAPER_ALTERNATIVE":
        return f"比方案里的《{name}》便宜 ${_price(other) - _price(item):.2f}"
    if code == "SAME_RELEASE_OTHER_EDITION":
        return f"和方案里的《{name}》是同一件，另一个版本（T1）"
    if code == "SAME_ARTIST":
        return f"和方案里的《{name}》同品牌/同艺人（T2/T3）"
    if code == "SAME_GENRE":
        return f"和方案里的《{name}》同一类型（T4/T5）"
    return f"和你搜过的「{history_text}」同方向" if history_text else "和本次检索方向相近"


def classify(
    item: Any,
    *,
    plan_items: list[Any] | None = None,
    anchor_tokens: set[str] | None = None,
    history_text: str = "",
) -> dict[str, Any]:
    """返回 {"code", "secondary", "text", "rel"}；code 为空串表示「没有可辩护的理由」。"""
    item_d = _as_dict(item)
    anchors = anchor_tokens or set()
    rel = relevance(item_d, anchors)
    plans = [_as_dict(p) for p in (plan_items or []) if p is not None]

    hits: list[tuple[str, str]] = []
    cats = {_category(p) for p in plans if _category(p)}
    own_cat = _category(item_d)
    if own_cat and own_cat not in cats and rel >= MIN_RELEVANCE:
        hits.append((
            "FILLS_CATEGORY_GAP",
            _text_for("FILLS_CATEGORY_GAP", item_d, item_d, history_text),
        ))

    tiered = [(substitution_tier(item_d, p)[0], p) for p in plans]

    cheaper: tuple[float, dict[str, Any]] | None = None
    for tier, p in tiered:
        saving = _price(p) - _price(item_d)
        if tier <= 4 and saving > 1e-9 and (cheaper is None or saving > cheaper[0]):
            cheaper = (saving, p)
    if cheaper is not None:
        hits.append((
            "CHEAPER_ALTERNATIVE",
            _text_for("CHEAPER_ALTERNATIVE", cheaper[1], item_d, history_text),
        ))

    for code, tiers in (
        ("SAME_RELEASE_OTHER_EDITION", (1,)),
        ("SAME_ARTIST", (2, 3)),
        ("SAME_GENRE", (4, 5)),
    ):
        for tier, p in tiered:
            if tier in tiers:
                hits.append((code, _text_for(code, p, item_d, history_text)))
                break

    if not hits and rel >= MIN_RELEVANCE:
        hits.append((
            "HISTORY_DIRECTION",
            _text_for("HISTORY_DIRECTION", item_d, item_d, history_text),
        ))

    # 按优先级排序（同一个码只留最早一条）
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for code, text in sorted(hits, key=lambda h: PRIORITY.index(h[0])):
        if code in seen:
            continue
        seen.add(code)
        ordered.append((code, text))

    if not ordered:
        return {"code": "", "secondary": None, "text": "", "rel": rel}
    primary, primary_text = ordered[0]
    secondary = ordered[1][0] if len(ordered) > 1 else None
    return {"code": primary, "secondary": secondary, "text": primary_text, "rel": rel}

