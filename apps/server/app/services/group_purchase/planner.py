"""双方案生成引擎 —— 权衡感知（Trade-off Aware）的方案生成。

对应执行清单 C 模块：
  C1 检索层适配：约束作为前置输入传给检索层，不是后置过滤
  C2 双方案生成：A（低价优先）/ B（耐用优先）
  C4 每套方案标注"牺牲了谁的诉求"（成员维度）
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

from app.services.group_purchase.budget_guardrail import calc_total
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.fallback import generate_fallback_message
from app.services.shopper.catalog_k1 import get_k1_items

logger = logging.getLogger(__name__)

# 关键词抽取辅助
_NEGATION_WORDS = ("不要", "不用", "拒绝", "禁止", "不能", "别用", "不含", "无", "避免", "no ", "without ")
_FILLER_WORDS = ("材质", "产品", "制品", "包装", "的", "东西", "物品")

def _extract_keywords(text: str) -> list[str]:
    """从约束文本中抽取可用于匹配的实体关键词。"""
    if not text:
        return []
    raw = text.strip().lower()
    for sep in ("、", ",", "，", ";", "；", "/", "和", "与"):
        raw = raw.replace(sep, "|")
    parts = [p.strip() for p in raw.split("|") if p.strip()]

    keywords: list[str] = []
    for part in parts:
        cleaned = part
        for neg in _NEGATION_WORDS:
            cleaned = cleaned.replace(neg, "")
        for filler in _FILLER_WORDS:
            cleaned = cleaned.replace(filler, "")
        cleaned = cleaned.strip()
        if cleaned:
            keywords.append(cleaned)
    return keywords


# 中文类目 → 英文检索扩展词。Channel3 是英文商品库，中文原句直检会命中大量无关商品
# （实测「买5个人团体的体育用品，球类」返回的是黑胶唱片/香水/身体喷雾）。
# 这里的映射用于**混入检索词**（实际发给 Channel3 的词），原句仍保留在推荐依据回显里。
_ZH_TO_EN_QUERY: tuple[tuple[str, str], ...] = (
    ("体育用品", "sports equipment"),
    ("运动", "sports"),
    ("球类", "ball"),
    ("足球", "soccer ball"),
    ("篮球", "basketball"),
    ("羽毛球", "badminton"),
    ("桌游", "board game"),
    ("棋盘", "board game"),
    ("卡牌", "card game"),
    ("奖品", "prize gift"),
    ("礼品", "gift"),
    ("文具", "stationery"),
    ("水杯", "water bottle"),
    ("水瓶", "water bottle"),
    ("保温瓶", "insulated bottle"),
    ("耳机", "headphones"),
    ("降噪", "noise cancelling"),
    ("音箱", "speaker"),
    ("唱片", "vinyl record"),
    ("黑胶", "vinyl record"),
    ("书", "book"),
    ("大衣", "coat"),
    ("外套", "jacket"),
    ("衣服", "clothing"),
    ("鞋", "shoes"),
    ("玩具", "toy"),
    ("环保", "eco friendly"),
    ("一次性", "disposable"),
    ("塑料", "plastic"),
    ("帆布", "canvas"),
    ("不锈钢", "stainless steel"),
    ("木质", "wooden"),
    ("铝", "aluminum"),
    ("横幅", "banner"),
    ("装饰", "decoration"),
)


def _query_tokens(text: str) -> set[str]:
    """检索 token：英文词（≥3 字符）+ 中文 2-gram。中英混合各自归一，避免「game」的
    局部子串误伤「Perfumed」这类无关英文商品。"""
    if not text:
        return set()
    low = text.lower()
    ascii_words = set(re.findall(r"[a-z0-9]{3,}", low))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", low))
    cjk_bigrams = {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    return ascii_words | cjk_bigrams


def _is_relevant_item(item: dict[str, Any], anchor_text: str) -> bool:
    """相关性闸门：商品文本（名称/标签/描述）与检索锚点是否真的沾边。"""
    anchors = _query_tokens(anchor_text)
    if not anchors:
        return True
    item_text = " ".join(
        str(item.get(k, "") or "") for k in ("name", "title", "category", "description")
    )
    tags = item.get("tags") or []
    if isinstance(tags, (list, tuple)):
        item_text = f"{item_text} {' '.join(str(t) for t in tags)}"
    elif tags:
        item_text = f"{item_text} {tags}"
    item_tokens = _query_tokens(item_text)
    for t in item_tokens:
        if t in anchors:
            return True
        # 允许词干级命中（headphone ↔ headphones / play ↔ playing），长度护栏防误伤
        if t.isascii() and len(t) >= 4:
            for a in anchors:
                if a.isascii() and len(a) >= 4 and (t.startswith(a) or a.startswith(t)):
                    return True
    return False


def build_search_query(intent_query: str) -> tuple[str, str, list[str]]:
    """把中文需求补成 Channel3 可检索的英文关键词。

    返回 (search_query, display_query, anchors)。display_query 用于推荐依据回显
    （保留用户原句，不假装检索词就是原句）；anchors 交给相关性闸门判定。
    """
    text = (intent_query or "").strip()
    anchors = _extract_keywords(text)
    english: list[str] = []
    for zh, en in _ZH_TO_EN_QUERY:
        if zh in text and en not in english:
            english.append(en)
    if not english:
        return text, text, anchors
    return f"{' '.join(english)} | {text}", text, anchors + english


def _overlap_score(item: dict[str, Any], anchors: set[str]) -> int:
    """「最接近」排序用：商品文本与检索锚点共有多少个 token。"""
    item_text = " ".join(
        str(item.get(k, "") or "") for k in ("name", "category", "description")
    )
    tags = item.get("tags") or []
    if isinstance(tags, (list, tuple)):
        item_text = f"{item_text} {' '.join(str(t) for t in tags)}"
    elif tags:
        item_text = f"{item_text} {tags}"
    return len(_query_tokens(item_text) & anchors)


async def _retrieve_catalog(
    query: str, use_channel3: bool
) -> tuple[list[dict[str, Any]], str, str, str, list[dict[str, Any]], str]:
    """检索商品目录：**优先 Channel3（赞助商）**，实在对不上才回落本地 catalog。

    返回 (catalog, source, search_query, note, nearest, channel3_status)。
      - search_query：真正发给 Channel3 的检索词（中英混检；回显用）
      - note：发生降级时的诚实说明（区分 error / empty / disabled）
      - nearest：Channel3 返回过但都不相关时，按 token 重合度取最接近的 5 件，
        作为「赞助商相关推荐」单独展示（不混进方案，避免把黑胶当体育用品卖）
      - channel3_status：ok / empty / error / disabled（进字段，供 UI 与审计消费）

    赞助商优先策略：
      1) 先打「英文关键词 | 用户原句」混合检索；
      2) 若 0 条相关，再打一次**纯英文关键词**（Channel3 对中文 query 召回很差，
         纯英文往往能救回来，这比直接把用户甩到本地示例目录更符合"赞助商优先"）；
      3) 两次都无相关才回落 k1_catalog，并附上最接近的 Channel3 商品。
    """
    search_query, display_query, anchors = build_search_query(query)
    english_only = " ".join(
        dict.fromkeys(en for zh, en in _ZH_TO_EN_QUERY if zh in (query or ""))
    )
    attempts = [search_query]
    if english_only and english_only != search_query:
        attempts.append(english_only)
    anchor_set = set(anchors)
    anchor_text = " ".join(anchors)

    nearest: list[dict[str, Any]] = []
    channel3_status = "disabled"
    if use_channel3:
        from app.services.channel3 import search_products_with_status

        channel3_status = "empty"
        for attempt in attempts:
            outcome = await search_products_with_status(attempt, limit=20)
            channel3_status = outcome.status
            if outcome.status in ("error", "disabled"):
                logger.warning(
                    "Channel3 状态=%s（%s），降级本地 catalog", outcome.status, outcome.message
                )
                break
            channel3_items = outcome.items
            if not channel3_items:
                logger.info("Channel3 检索词 %r 命中 0 条（empty）", attempt)
                continue
            relevant = [it for it in channel3_items if _is_relevant_item(it, anchor_text)]
            # 同名同价去重：Channel3 会为不同 SKU 返回几乎相同的标题（例如
            # 「Downtown Basketball - Classic Brown」连出两次），方案里重复出现同一件商品
            # 会被误读成"买了两件"，也让 A/B 差异对比更难读。
            deduped: list[dict[str, Any]] = []
            seen_keys: set[tuple[str, int]] = set()
            for it in relevant:
                name = str(it.get("name") or it.get("title") or "").strip().lower()
                key = (name, int(round(float(it.get("price") or 0) * 100)))
                if name and key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped.append(it)
            relevant = deduped
            logger.info(
                "Channel3 搜索命中 %d 条 / 相关 %d 条（search_query=%r, display=%r）",
                len(channel3_items), len(relevant), attempt, display_query,
            )
            if relevant:
                note = (
                    f"部分候选不相干，已过滤 {len(channel3_items) - len(relevant)} 件"
                    if len(relevant) < len(channel3_items)
                    else ""
                )
                return relevant, "channel3", attempt, note, [], "ok"
            if not nearest:
                nearest = sorted(
                    channel3_items, key=lambda it: _overlap_score(it, anchor_set), reverse=True
                )[:5]
            logger.warning(
                "Channel3 检索词 %r 命中 %d 条但都与需求无关，%s",
                attempt, len(channel3_items),
                "换纯英文检索词重试" if attempt != attempts[-1] else "诚实回落本地目录",
            )

    # 三态文案：error 是"暂时不可用"（可重试），empty 是"商品库真没匹配"（换词才有用）。
    if channel3_status == "error":
        note = (
            "赞助商 Channel3 检索暂时不可用（error），已切换到本地示例目录；"
            "可点「🔄 重新生成方案」重试"
        )
    elif channel3_status == "disabled":
        note = "Channel3 检索未启用（disabled），本次使用本地示例目录"
    else:
        note = "赞助商 Channel3 商品库中未找到与「%s」匹配的商品（empty），已切换到本地示例目录" % (
            display_query or "该需求"
        )
    logger.info("使用本地 catalog 作为后备（channel3_status=%s）", channel3_status)
    return get_k1_items(), "k1_catalog", search_query, note, nearest, channel3_status


async def generate_dual_plans(
    intent_query: str,
    constraints: list[Constraint],
    min_items: int = 3,
    max_items: int = 8,
    use_channel3: bool = True,
) -> dict[str, Any]:
    """生成双方案（低价优先 A / 耐用优先 B）。

    统一思想：A 和 B 从**同一个候选池**里挑货，B 不是从另一个宇宙拿货。
    当候选来自 Channel3 时，B 的每个 slot 都在 A 的基础上做「同品类升级」：
    要么换成关键词匹配分更高的同款，要么保持原物 —— 因此「共有商品」天然 > 0，
    差异对比展示的是逐件升级，而不是两篮完全不同的商品。
    """
    budget_constraints = [c for c in constraints if c.type == "budget" and c.status == "active"]
    budget_caps = [c.numeric_value for c in budget_constraints if c.numeric_value]
    budget_limit = min(budget_caps) if budget_caps else None

    (
        catalog,
        source,
        search_query,
        retrieval_note,
        nearest,
        channel3_status,
    ) = await _retrieve_catalog(intent_query, use_channel3)
    filtered = prefilter_by_constraints(catalog, constraints)

    if not filtered:
        return {
            "feasible": False,
            "plan_a": None,
            "plan_b": None,
            "source": source,
            "fallback": generate_fallback_message(
                constraints, catalog, [c.id for c in constraints if c.hard], budget_limit
            ),
            "comparison": {},
            "retrieval": _retrieval_meta(
                intent_query, source, catalog, filtered,
                search_query, retrieval_note, nearest, channel3_status,
            ),
        }

    # Channel3 命中的商品是异构的：B 以 A 为底座做「同品类升级」，最大化共有商品。
    # 注意：本地兜底目录刻意不共享底座——那是「A 低价 / B 匹配」的对照锚点，
    # 共享后会退化成「B 只是 A 的微调」，把 B 的推荐性格与 393 预算锚点一起抹掉。
    share_basis = source == "channel3"

    plan_a = _assemble_strategy_plan(
        candidates=filtered,
        strategy="cheap",
        budget_limit=budget_limit,
        constraints=constraints,
        max_items=max_items,
        min_items=min_items,
    )
    plan_b = _assemble_strategy_plan(
        candidates=filtered,
        strategy="relevance",
        intent_query=intent_query,
        budget_limit=budget_limit,
        constraints=constraints,
        max_items=max_items,
        min_items=min_items,
        share_basis=share_basis,
        basis_items=plan_a.get("items", []),
    )

    # 跨方案取舍：把价差说成「多花了多少、换来了什么」，用户才能判断值不值
    # （放在双方案都生成之后，所以这里的数字是真实对比，不是拍脑袋）
    delta_ab = round(plan_b["total"] - plan_a["total"], 2)
    if delta_ab:
        plan_a["trade_offs"] = list(plan_a.get("trade_offs") or []) + [
            f"相比方案 B 少花 ${delta_ab:.2f}（A ${plan_a['total']:.2f} vs B ${plan_b['total']:.2f}）"
        ]
        plan_b["trade_offs"] = list(plan_b.get("trade_offs") or []) + [
            f"相比方案 A 多花 ${delta_ab:.2f}，换来同品类里更贴题的 {len(plan_b['items'])} 件"
        ]

    # J 模块：生成后二次检查——任一方案为空说明无解，触发兜底
    if not plan_a["items"] or not plan_b["items"]:
        return {
            "feasible": False,
            "plan_a": None,
            "plan_b": None,
            "source": source,
            "fallback": generate_fallback_message(constraints, filtered, [], budget_limit),
            "comparison": {},
            "retrieval": _retrieval_meta(
                intent_query, source, catalog, filtered,
                search_query, retrieval_note, nearest, channel3_status,
            ),
        }

    return {
        "feasible": True,
        "plan_a": plan_a,
        "plan_b": plan_b,
        "source": source,
        "comparison": _compare_plans(plan_a, plan_b, budget_limit),
        "retrieval": _retrieval_meta(
            intent_query, source, catalog, filtered,
            search_query, retrieval_note, nearest, channel3_status,
        ),
    }


def _retrieval_meta(
    intent_query: str,
    source: str,
    catalog: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
    search_query: str = "",
    note: str = "",
    sponsor_related: list[dict[str, Any]] | None = None,
    channel3_status: str = "",
) -> dict[str, Any]:
    """检索证据：用户看到的「推荐依据」就是这份元数据的镜像。

    - query：用户原句（推荐依据回显用，保证「依据」就是用户的话）
    - search_query：实际发给 Channel3 的检索词（中英混检；诚实呈现，不假装检索词=原句）
    - source：channel3 / k1_catalog
    - candidate_count / matched_count：命中池规模与约束过滤后的候选数
    - note：检索层明明相关商品时的诚实说明（如全量不相干 → 回落本地目录）
    - sponsor_related：赞助商 Channel3 检索到但「不完全匹配」的商品（回落本地时展示，
      单独成卡，标注「最接近」，绝不混进方案冒充匹配命中）
    - channel3_status：ok / empty / error / disabled（三态进字段，不只进文案）
    """
    related = [
        {
            "id": str(it.get("id", "")),
            "name": str(it.get("name") or it.get("title") or ""),
            "price": float(it.get("price", 0) or 0),
            "currency": str(it.get("currency") or "USD"),
            "category": str(it.get("category") or ""),
            "image_url": str(it.get("image_url") or ""),
            "tags": [str(t) for t in (it.get("tags") or [])],
        }
        for it in (sponsor_related or [])
        if it.get("id") and (it.get("name") or it.get("title"))
    ][:5]
    return {
        "query": intent_query or "",
        "search_query": search_query or intent_query or "",
        "source": source,
        "candidate_count": len(catalog),
        "matched_count": len(filtered),
        "note": note,
        "channel3_status": channel3_status,
        "sponsor_related": related,
        "sponsor_note": (
            "Channel3（赞助商）没有与本次需求完全匹配的商品，以下是它检索到的最接近的几件，"
            "仅供单独参考，未计入方案"
            if related
            else ""
        ),
    }


def _assemble_strategy_plan(
    candidates: list[dict[str, Any]],
    strategy: Literal["cheap", "relevance"],
    budget_limit: float | None,
    constraints: list[Constraint],
    max_items: int = 8,
    min_items: int = 3,
    intent_query: str = "",
    share_basis: bool = False,
    basis_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A 按价格排序；B 按关键词证据排序。价格高低不代表耐用或质量。

    share_basis=True 时（Channel3 候选），B 不再另起一篮，而是拿 A 的每一件
    做「同品类升级」：同品类里关键词匹配分更高的候选替换它；没有更好候选就原样保留。
    这样 diff 视图里「共有商品」是真实存在的，两方案的价差也能逐件归因。
    """
    from app.services.shopper.ranker import rank_candidates

    plan_id = "A" if strategy == "cheap" else "B"
    strategy_name = "低价优先" if strategy == "cheap" else "匹配优先"
    ranked = rank_candidates(
        candidates=candidates,
        taste_anchors=[intent_query] if intent_query else [],
        preferred_categories=[],
        budget=budget_limit,
    )
    relevance = {item["id"]: item for item in ranked}

    if share_basis and basis_items:
        selected, spent = _upgrade_basis_items(
            basis_items=basis_items,
            candidates=candidates,
            relevance=relevance,
            budget_limit=budget_limit,
            max_items=max_items,
            min_items=min_items,
        )
    else:
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (
                float(c.get("price", 0) or 0) if strategy == "cheap"
                else -float(relevance.get(c["id"], {}).get("score", 0))
            ),
        )

        selected: list[dict[str, Any]] = []
        spent = 0.0

        for item in sorted_candidates:
            price = item.get("price", 0)
            if budget_limit is not None and spent + price > budget_limit:
                continue
            selected.append(item)
            spent += price
            if len(selected) >= max_items:
                break

        # 若商品太少，尝试补充最便宜的
        if len(selected) < min_items:
            selected_ids = {it["id"] for it in selected}
            for item in sorted(candidates, key=lambda c: c.get("price", 0)):
                if item["id"] in selected_ids:
                    continue
                price = item.get("price", 0)
                if budget_limit is not None and spent + price > budget_limit:
                    continue
                selected.append(item)
                spent += price
                selected_ids.add(item["id"])
                if len(selected) >= min_items:
                    break

    # 构建 PlanItem 列表
    items = []
    for item in selected:
        source_val = item.get("source", "local_catalog")
        items.append({
            "id": item["id"],
            "name": item["name"],
            "category": item.get("category", ""),
            "price": item.get("price", 0),
            "quantity": 1,
            "subtotal": item.get("price", 0),
            "matched_constraints": [],
            "source": source_val,
            "image_url": item.get("image_url", ""),
            "tags": list(item.get("tags", []) or []),
            "match_score": relevance.get(item["id"], {}).get("score", 0),
            "match_tags": relevance.get(item["id"], {}).get("match_tags", []),
            "rationale": relevance.get(item["id"], {}).get("rationale", "该方案按价格从低到高选择" if strategy == "cheap" else "暂无直接关键词匹配证据"),
        })

    total = calc_total(items)

    matched = []
    unmet = []
    for c in constraints:
        if c.status != "active":
            continue
        if _check_constraint_match(item_list=items, constraint=c):
            matched.append(c.id)
        else:
            unmet.append(c.id)

    trade_offs = _generate_trade_offs(strategy_name, items, total, budget_limit, unmet, intent_query)
    risks = _generate_risks(strategy_name, total, budget_limit)

    return {
        "plan_id": plan_id,
        "strategy": strategy_name,
        "items": items,
        "total": round(total, 2),
        "currency": "USD",
        "matched_constraints": matched,
        "unmet_constraints": unmet,
        "trade_offs": trade_offs,
        "risks": risks,
    }


def _family_key(item: dict) -> str:
    """同品类分组键：两方案在同一个品类内做「升级」而不是在品类间换血。

    Channel3 的商品 category 为空时归入 general —— 此时仍能保证所有候选同池，
    只是升级粒度退化为「全池最优替换」。
    """
    cat = str(item.get("category") or "").strip().lower()
    return cat or "general"


def _upgrade_basis_items(
    *,
    basis_items: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    relevance: dict[str, dict[str, Any]],
    budget_limit: float | None,
    max_items: int,
    min_items: int = 1,
) -> tuple[list[dict[str, Any]], float]:
    """B 在 A 的底座上做逐件升级：

    - 每个 slot 优先保留 A 的原件；
    - 只有当同品类存在「关键词匹配分更高（或同分但更贵）」的候选，
      且加进预算仍装得下时，才换成它；
    - 超预算时回退原件（A 本身就是预算内可行解，B 不会比 A 更不可行）。

    返回 (selected, spent)。spent 只是预算内可行性的近似，不影响最终 total 计算。
    """
    cand_by_id = {c["id"]: c for c in candidates}
    families: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        families.setdefault(_family_key(c), []).append(c)

    used: set[str] = set()
    selected: list[dict[str, Any]] = []
    spent = 0.0

    def _score(item: dict) -> float:
        return float(relevance.get(item.get("id", ""), {}).get("score", 0) or 0)

    for base in basis_items:
        if len(selected) >= max_items:
            break
        base_cand = cand_by_id.get(base.get("id", ""), base)
        if base_cand.get("id") in used:
            continue
        family = _family_key(base_cand)
        base_score = _score(base_cand)
        base_price = float(base_cand.get("price", 0) or 0)

        alts = [
            c for c in families.get(family, [])
            if c.get("id") != base_cand.get("id") and c.get("id") not in used
        ]
        alts.sort(key=lambda c: (-_score(c), -float(c.get("price", 0) or 0)))

        chosen = base_cand
        for alt in alts:
            alt_score = _score(alt)
            alt_price = float(alt.get("price", 0) or 0)
            better = alt_score > base_score + 1e-9 or (
                abs(alt_score - base_score) < 1e-9 and alt_price > base_price
            )
            if not better:
                continue
            if budget_limit is not None and spent + alt_price > budget_limit:
                continue
            chosen = alt
            break

        price = float(chosen.get("price", 0) or 0)
        # 升级后超预算：回退 A 原件（A 是预算内可行解）
        if budget_limit is not None and spent + price > budget_limit:
            chosen = base_cand
            price = base_price
            if budget_limit is not None and spent + price > budget_limit:
                continue
        selected.append(chosen)
        used.add(chosen.get("id"))
        spent += price

    # 数量不足时用「匹配优先」补位，保持 B 的推荐性格
    if len(selected) < min_items:
        rest = sorted(
            candidates,
            key=lambda c: (-_score(c), float(c.get("price", 0) or 0)),
        )
        for c in rest:
            if len(selected) >= min_items:
                break
            if c.get("id") in used:
                continue
            price = float(c.get("price", 0) or 0)
            if budget_limit is not None and spent + price > budget_limit:
                continue
            selected.append(c)
            used.add(c.get("id"))
            spent += price

    return selected, spent


def _check_constraint_match(item_list: list[dict], constraint: Constraint) -> bool:
    """检查选中的商品组合是否满足某条约束。"""
    ctype = constraint.type
    ccontent = (constraint.content or "").lower()
    tags_blob = " ".join(str(t).lower() for it in item_list for t in (it.get("tags", []) or []))
    names_blob = " ".join(it["name"].lower() for it in item_list)

    if ctype == "budget":
        return True

    if ctype == "taboo":
        for keyword in _extract_keywords(ccontent) or [k.strip() for k in ccontent.split("、") if k.strip()]:
            if keyword in tags_blob or keyword in names_blob:
                return False
        return True

    if ctype == "functional":
        for keyword in _extract_keywords(ccontent) or [k.strip() for k in ccontent.split("、") if k.strip()]:
            if keyword in tags_blob or keyword in names_blob:
                return True
        return False

    return True


def _generate_trade_offs(
    strategy_name: str,
    items: list[dict[str, Any]],
    total: float,
    budget_limit: float | None,
    unmet_constraints: list[str],
    intent_query: str = "",
) -> list[str]:
    """生成方案的取舍说明——必须是**这批商品的实际数字**，不是通用免责声明。

    旧版写的是「选择了平价款，可能在耐用性和品牌上有所妥协」这类空话：
    用户看不出和方案有什么关系，也没法用来判断要不要点确认。
    Channel3 只给 标题 / 价格 / 规格与品牌标签（没有评分、评论），
    所以我们能诚实地说清「差在哪、差多少、用什么换来的」。
    """
    trades: list[str] = []
    count = len(items)
    avg = total / count if count else 0.0
    tagged = sum(1 for it in items if it.get("tags"))

    if unmet_constraints:
        trades.append(f"{len(unmet_constraints)} 条约束这批商品没满足")

    if strategy_name == "低价优先":
        trades.append(f"{count} 件都取候选里最便宜的，均价 ${avg:.2f}、合计 ${total:.2f}")
        if budget_limit and budget_limit > 0:
            trades.append(f"只用了预算 {total / budget_limit * 100:.0f}%，余 ${budget_limit - total:.2f} 可留作加购或运费")
        if count and tagged < count:
            trades.append(f"其中 {count - tagged} 件没有品牌/规格标签，只能按价格判断")
    else:
        q = intent_query.strip() or "本次检索词"
        trades.append(f"{count} 件按与「{q}」的关键词相关度从高到低，均价 ${avg:.2f}、合计 ${total:.2f}")
        trades.append("排序只用标题/价格/品牌规格标签（该数据源无评分与评论）：更贴题，但不等于更耐用")

    return trades


def _generate_risks(strategy_name: str, total: float, budget_limit: float | None) -> list[str]:
    """生成潜在风险提示。"""
    risks = []
    if budget_limit is not None and total > budget_limit * 0.95:
        risks.append(f"总价 {total:.2f} 接近预算上限 {budget_limit:.2f}，价格波动可能导致超支")

    if strategy_name == "匹配优先":
        risks.append("关键词相似度不等于真实适配度，请核对商品描述与图片")

    risks.append("最终支付需用户在 PayPal 页面点击确认，AI 无支付发起权")
    return risks


def _compare_plans(plan_a: dict, plan_b: dict, budget_limit: float | None) -> dict:
    """比较两个方案的差异。

    统一思想：两套方案来自同一候选池，因此给出 shared_count / 逐件价差归因，
    而不是两篮无关商品之间的金额对比。
    """
    items_a = plan_a.get("items", [])
    items_b = plan_b.get("items", [])
    ids_b = {it.get("id") for it in items_b}
    shared = [it for it in items_a if it.get("id") in ids_b]
    # 逐件升级：B 是 A 的「同品类升级」底座（_upgrade_basis_items 保证 slot 一一对应）。
    # 因此按槽位 zip 归因价差，而不是按 id 配对——同一件商品（同 id）不是升级，
    # 否则会出现 "一次性塑料袋 1.5→1.5 +0.00" 这种无意义条目。
    upgrades = []
    for slot, base in enumerate(items_a):
        if slot >= len(items_b):
            break
        it = items_b[slot]
        if it.get("id") == base.get("id"):
            continue  # 原样保留，非升级
        upgrades.append(
            {
                "item": base.get("name", ""),
                "category": base.get("category", ""),
                "from": base.get("price", 0),
                "to": it.get("price", 0),
                "delta": round(float(it.get("price", 0)) - float(base.get("price", 0)), 2),
            }
        )
    return {
        "total_delta": round(plan_b["total"] - plan_a["total"], 2),
        "item_count_a": len(items_a),
        "item_count_b": len(items_b),
        "shared_count": len(shared),
        "only_a_count": len(items_a) - len(shared),
        "only_b_count": len(items_b) - len(shared),
        "upgrades": upgrades,
        "budget_limit": budget_limit,
        "recommendation": (
            "建议方案 A（低价优先），适合预算敏感场景"
            if plan_a["total"] <= (budget_limit or float("inf")) * 0.9
            else "建议方案 B（同品类关键词匹配优先）；请查看每件商品的匹配证据"
        ),
    }


def prefilter_by_constraints(catalog: list[dict[str, Any]], constraints: list[Constraint]) -> list[dict[str, Any]]:
    """C1：基于硬约束前置过滤商品目录。"""
    if not catalog:
        return []

    hard_constraints = [c for c in constraints if c.status == "active" and c.hard]
    if not hard_constraints:
        return list(catalog)

    filtered: list[dict[str, Any]] = []

    # Quantity-type functional constraints (e.g. "5 人份") describe cart-level needs, not
    # item attributes. Filtering items by them empties the whole catalog. Handle them at
    # plan-assembly time instead.
    functional_constraints = [c for c in hard_constraints if c.type == "functional"]
    # Quantity-type constraints may be phrased many ways by the LLM extractor
    # ("5 人份", "社团 5 人", "need 5 sets", "5-piece"). Treat any functional
    # constraint carrying a number as cart-level quantity info: it describes how
    # much to buy, not which item attributes are required.
    quantity_only = all(
        _is_quantity_constraint(c)
        for c in functional_constraints
    ) if functional_constraints else False

    for item in catalog:
        item_blob = " ".join(str(item.get(k, "")) for k in ("name", "description", "category")).lower()
        tags_blob = " ".join(str(t).lower() for t in (item.get("tags", []) or []))
        full_blob = f"{item_blob} {tags_blob}"

        passed = True
        for c in hard_constraints:
            if c.type == "budget":
                if c.numeric_value is not None and item.get("price", 0) > c.numeric_value:
                    passed = False
                    break
            elif c.type == "taboo":
                keywords = _extract_keywords(c.content)
                if keywords:
                    for kw in keywords:
                        if kw in full_blob:
                            passed = False
                            break
                if not passed:
                    break
            elif c.type == "functional" and not quantity_only:
                keywords = _extract_keywords(c.content) or [k.strip() for k in c.content.split("、") if k.strip()]
                if keywords:
                    matched = any(kw in full_blob for kw in keywords)
                    if not matched:
                        passed = False
                        break

        if passed:
            filtered.append(item)

    return filtered


def _is_quantity_constraint(c: Constraint) -> bool:
    """功能约束是否为数量/人数类（购物车层面信息，不应过滤单品）。"""
    text = (c.content or "").lower()
    if c.numeric_value is not None and c.numeric_value <= 100:
        return True
    return any(
        kw in text for kw in ("人份", "person", "people", "qty", "quantity", "份", "人数", "个", "人", "set")
    )