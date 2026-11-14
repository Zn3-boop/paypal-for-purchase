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
from app.services.group_purchase.constraints import Constraint, total_budget_cap
from app.services.group_purchase.fallback import generate_fallback_message
from app.services.group_purchase.satisfaction import evaluate, item_satisfies
from app.services.shopper.catalog_k1 import get_k1_items
from app.services.group_purchase.tiering import (
    MAX_TOTAL_UPLIFT,
    MAX_UPGRADE_TIER,
    price_uplift_ok,
    substitution_tier,
    tier_label,
)

logger = logging.getLogger(__name__)

# B 最多替换 A 的几件。旧行为是「每个槽位都换」：只要同品类有匹配分更高的候选就替换，
# 于是 B 必然变成「另一批更贵的货」（8/8 全换、交集 0 件），读起来像换货而不是局部升级。
DEFAULT_MAX_UPGRADES = 3

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
    ("充电宝", "power bank"),
    ("移动电源", "power bank"),
    ("快充", "fast charging"),
    ("充电器", "charger"),
    ("便携", "portable"),
    ("背包", "backpack"),
    ("帆布包", "canvas bag"),
    ("帽子", "hat"),
    ("手表", "watch"),
    ("眼镜", "glasses"),
    ("毛巾", "towel"),
    ("牙刷", "toothbrush"),
    ("纸巾", "paper towel"),
    ("雨伞", "umbrella"),
    ("保温杯", "insulated cup"),
    ("零食", "snack"),
    ("茶叶", "tea"),
    ("帐篷", "tent"),
    ("睡袋", "sleeping bag"),
    ("手电筒", "flashlight"),
    # 评审 Bug 3 高频缺口（2026-11-10 用户实测三连无解）：
    # 静音键盘 / 牛仔裤 / 围巾 —— 用户用中文整句直检 Channel3，这些词不在映射表
    # 时英文池为 0，直接降级本地 45 件 K1 示例目录（没有键盘/牛仔裤/围巾）→ 必然 0 命中。
    ("键盘", "keyboard"),
    ("静音", "silent"),
    ("按键", "key switch"),
    ("顺滑", "smooth"),
    ("浅灰", "light gray light grey"),
    ("灰色", "gray grey"),
    # 2026-11-14 实测：颜色出检索词的口径必须和判定词典（satisfaction.COLOR_KEYWORDS）对得上
    # —— 用户原句是「银灰色台灯/深空灰充电宝/米白色保温杯」，如果检索词里没有银色/深空灰/米白
    # 的英文，Channel3 召回就不会带颜色属性，判定侧再强的词典也「无证可查」。
    ("银灰", "silver gray silver grey"),
    ("深空灰", "dark gray dark grey gunmetal"),
    ("米白", "ivory off-white cream"),
    ("墨黑", "black matte black"),
    ("蓝牙", "bluetooth"),
    ("无线", "wireless"),
    ("耳机", "headphones earphones"),
    ("牛仔裤", "jeans"),
    ("高腰", "high rise"),
    ("水洗蓝", "washed blue"),
    ("直筒", "straight leg"),
    ("掉色", "colorfast fade-resistant"),
    ("围巾", "scarf"),
    ("羊毛", "wool"),
    ("焦糖色", "caramel"),
    ("宽版", "wide"),
    ("保暖", "warm thermal"),
    # 演示脚本 B4（马克杯，2026-11-10 实测 Channel3 empty）：只补这一条用到的词
    ("马克杯", "mug"),
    ("陶瓷", "ceramic"),
    ("奶白", "cream off-white"),
    # ── 20 条模板全量审计（2026-11-10）──────────────────────────────
    # 教训（P0-3）：检索词典和判定词典必须成对补。此前「雨伞」检索认识（umbrella）、
    # 判定不认识，「数据线」两边都不认识（整句中文直检 → 0 命中、全是无关商品）。
    # 用户看到的是「检索一堆、取证全 ❔」，等于没查。
    ("半身裙", "skirt"),
    ("裙", "skirt"),
    ("开衫", "cardigan"),
    ("针织", "knit"),
    ("皮鞋", "leather shoes"),
    ("数据线", "usb cable charging cable"),
    ("编织", "braided"),
    ("藏青", "navy"),
    ("墨绿", "dark green"),
    ("哑光", "matte"),
    ("磨砂", "matte"),
    ("人体工学", "ergonomic"),
    ("小容量", "compact"),
    ("宽口", "wide mouth"),
    ("垂感", "drape"),
    ("挺括", "structured"),
    ("抗皱", "wrinkle resistant"),
    ("抗风", "windproof"),
    ("防水", "waterproof"),
    ("加固", "reinforced"),
    ("加厚", "heavy duty"),
    ("折叠", "foldable"),
    ("耐磨", "durable"),
    ("保温", "insulated thermal"),
    ("加大", "extra large"),
    ("托特", "tote bag"),
    ("低跟", "low heel"),
    ("软糯", "soft"),
    ("亲肤", "soft"),
    ("一米", "1m"),
    # ── 单人模板（2026-11-13 现场实测）：台灯需求只映射出 gray，检索回一堆灰色
    # 装饰灯，与「全光谱护眼 / 可触控调光」毫无关系。检索词必须覆盖功能词。
    ("台灯", "desk lamp table lamp"),
    ("桌面台灯", "desk lamp"),
    ("护眼", "eye care eye protection"),
    ("全光谱", "full spectrum"),
    ("触控", "touch"),
    ("调光", "dimmable dimmer"),
    # 笔记本（Channel3 有 notebook/journal 品类，此前没有映射 → 直接 empty 降级本地）
    ("笔记本", "notebook journal"),
    ("道林", "paper"),
    ("锁线", "stitched bound"),
    # ── 鞋服/箱包（2026-11-13 实测）：运动鞋场景英文池只有 `sports shoes`、
    # 斜挎包场景只有 `waterproof` —— 用户说的 5 条要求里 4 条没进检索词，
    # 于是召回一堆跑鞋/女鞋/童鞋，团建「统一队鞋」组不成队。
    ("运动鞋", "running shoes sneakers"),
    ("跑鞋", "running shoes"),
    ("防滑", "non-slip anti-slip grip"),
    ("透气", "breathable"),
    ("网面", "mesh upper"),
    ("拼色", "colorblock two-tone"),
    ("黑白", "black white"),
    ("尺码", "size fit"),
    ("斜挎", "crossbody messenger shoulder bag"),
    ("小包", "small bag pouch"),
    ("尼龙", "nylon"),
    ("橄榄绿", "olive green"),
    ("轻便", "lightweight"),
    ("通勤", "commuter everyday carry"),
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
    # 杯具语境互斥（2026-11-14 评审 P1-①）：用户说「陶瓷马克杯…工作室日常水杯」时，
    # 「水杯」是杯具同义语而非水瓶 —— 若文本已含杯具体词，则「水杯」翻译为 cup，
    # 不再翻译成 water bottle（实测该污染把 Channel3 陶瓷杯候选打到只剩 5/19）。
    _CUP_KIND = ("马克杯", "陶瓷杯", "咖啡杯", "茶杯", "骨瓷", "瓷杯", "带杯盖")
    _cup_context = any(w in text for w in _CUP_KIND)
    for zh, en in _ZH_TO_EN_QUERY:
        if zh in text and en not in english:
            if zh == "水杯" and _cup_context and "cup" not in english:
                english.append("cup")
            else:
                english.append(en)
    # 容量/功率/规格数字直接进检索池（数字+单位不需要词典）：用户要 500ml / 20W / 10000mAh，
    # 只拿「保温杯」或「充电宝」去搜会召回一堆 40oz 超大杯或无快充的杂牌（2026-11-14 实测
    # 「20W 快充 10000mAh」的检索词只有 power bank portable，20 候选只召回 2 件）。
    for m in re.finditer(r"(\d{2,5})\s*(ml|毫升|w|瓦|mah|毫安时|wh|瓦时)", text, re.I):
        token = f"{m.group(1)}{m.group(2).lower()}"
        if token not in english:
            english.append(token)
    m = re.search(r"(?:^|[^\d])(316|304|18/8)\s*(?:不锈钢|stainless)", text, re.I)
    if m and m.group(1) not in english:
        english.append(m.group(1))
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
    query: str, use_channel3: bool, force_probe: bool = False
) -> tuple[list[dict[str, Any]], str, str, str, list[dict[str, Any]], str, int]:
    """检索商品目录（Channel3 优先，失败降级本地 K1）。

    返回 7 元组：
        catalog         – 进入方案池的商品（channel3=去重后相关池 / k1=全量目录）
        source          – channel3 / k1_catalog
        search_query    – 实际检索词（中英混检）
        note            – 检索层定性说明（无数字口径，数字见 _retrieval_meta）
        nearest         – 赞助商「最接近但不完全匹配」商品（回落本地时展示）
        channel3_status – ok / empty / error / disabled
        recalled_count  – 原始召回条数（候选数唯一来源：channel3=API 返回条数 / k1=目录总条数）
    """
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
            outcome = await search_products_with_status(
                attempt, limit=20, force_probe=force_probe
            )
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
                # 注：过滤数字不再写进 note —— 候选/命中/过滤三数由 _retrieval_meta 结构化
                # 字段表达（recalled → relevant(dedup) → 约束预筛），note 只留定性说明。
                # 旧版在这里写死「已过滤 K 件（=原始召回−相关）」但 candidate_count 却是
                # 相关池后的大小，两级口径混用导致「候选 19/命中 19/过滤 1」这类心算矛盾。
                note = "部分候选与需求相关性低，已从候选中剔除" if len(relevant) < len(channel3_items) else ""
                return relevant, "channel3", attempt, note, [], "ok", len(channel3_items)
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
    k1_items = get_k1_items()
    return k1_items, "k1_catalog", search_query, note, nearest, channel3_status, len(k1_items)


async def generate_dual_plans(
    intent_query: str,
    constraints: list[Constraint],
    min_items: int = 3,
    max_items: int = 8,
    use_channel3: bool = True,
    force_probe: bool = False,
) -> dict[str, Any]:
    """生成双方案（低价优先 A / 耐用优先 B）。

    统一思想：A 和 B 从**同一个候选池**里挑货，B 不是从另一个宇宙拿货。
    当候选来自 Channel3 时，B 的每个 slot 都在 A 的基础上做「同品类升级」：
    要么换成关键词匹配分更高的同款，要么保持原物 —— 因此「共有商品」天然 > 0，
    差异对比展示的是逐件升级，而不是两篮完全不同的商品。

    force_probe（半开探针）：用户显式点「🔄 重新生成方案」时由 API 层传入 True，
    透传给 Channel3 检索 —— 熔断期内也放行一次真实探测。成功即关熔断 + 入缓存，
    失败维持短路。不加它时行为与以前完全一致（熔断照常短路自动重试）。
    """
    # Bug 1（评审坑 3）：组合总价 cap 只取 scope="total" 的预算。
    # per_item（「单件预算不超 130」）约束的是每一件单价，不约束组合总价。
    # 7 个消费点统一走 total_budget_cap，避免各处各写一遍 min()（2026-11-13 实测
    # 就是漏看 scope 导致「单件 ≤ $240 × 32 双」被算成 491% 超支）。
    budget_limit = total_budget_cap(constraints)

    (
        catalog,
        source,
        search_query,
        retrieval_note,
        nearest,
        channel3_status,
        recalled_count,
    ) = await _retrieve_catalog(intent_query, use_channel3, force_probe=force_probe)
    filtered = prefilter_by_constraints(
        catalog,
        constraints,
        semantic_pool=(source == "channel3"),
        # Bug 3（评审）：本地目录 0 命中 ≠ 无解 —— 大多是 K1 示例目录（45 件）没覆盖
        # 键盘/牛仔裤/围巾这类品类。退回预算/禁忌首遍池展示全部候选，由 satisfaction 判 ❔。
        empty_fallback_to_base=(source == "k1_catalog"),
    )

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
                search_query, retrieval_note, nearest, channel3_status, recalled_count,
            ),
        }

    # Channel3 命中的商品是异构的：B 以 A 为底座做「同品类升级」，最大化共有商品。
    # 注意：本地兜底目录刻意不共享底座——那是「A 低价 / B 匹配」的对照锚点，
    # 共享后会退化成「B 只是 A 的微调」，把 B 的推荐性格与 393 预算锚点一起抹掉。
    share_basis = source == "channel3"

    # 数量词硬约束：用户说「一件大衣」→ 方案只给 1 件；说「5 人份」→ 5 件。
    # 解析到明确数量才 cap min/max，没提数量的（团购/展示场景）维持调用方传入的默认。
    # 评审坑（2026-11-10）：旧逻辑 min(max_items, qty) 会把「12 把」静默压成 8 件 ——
    # 数量是用户明确诉求，只降不升等于撒谎。改为 max_items = qty（批量采购给足件数），
    # 若池子里符合条件的不足 qty，由下方 quantity_note 诚实说明「需 12 件仅找到 N 件」。
    qty = _parse_requested_quantity(intent_query or "")
    requested_qty = qty
    if qty is not None:
        max_items = max(qty, 1)
        min_items = min(min_items, qty)

    # 「同款×N」批量触发（2026-11-14）：统一/一致/同款/队 + 明确硬数量 → 从
    # 「挑 N 个不同 SKU 各 1 件」改为「选最优 1 个 SKU × N」；N 人份软数量排除。
    strong_unified = _unified_batch_qty(intent_query or "")
    # 团购弱信号（P1-②，2026-11-14）：无统一词但「团体/团购/一批…」语境 + N>1 +
    # 无反意词 → 同样默认同款×N（评审实测「采购 40 只马克杯 → 5 个不同款」是语义落差）。
    unified_qty = strong_unified if strong_unified is not None else _group_unified_qty(intent_query or "")
    unified_strict = strong_unified is not None
    if unified_qty is not None:
        # 同款×N 下件数由 quantity 承载：不再用 max_items 撑不同 SKU 数，
        # 也不让 min_items=3 把单行主款撑成三行。
        max_items = max(max_items, 1)
        min_items = min(min_items, 1)

    plan_a = _assemble_strategy_plan(
        candidates=filtered,
        strategy="cheap",
        budget_limit=budget_limit,
        constraints=constraints,
        max_items=max_items,
        min_items=min_items,
        unified_qty=unified_qty,
        unified_strict=unified_strict,
    )
    plan_a["source"] = source
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
        unified_qty=unified_qty,
        unified_strict=unified_strict,
    )
    plan_b["source"] = source

    # 数量诚实（评审盯点②）：用户说「12 把」但候选池只凑得出 N 件时，明说 + 影响可行性。
    # 只写 quantity_note 不把 feasible 置 False 等于「显示可行、实际凑不足」——这是新的撒谎。
    # plan 级 feasible=False：前端据此展示警示条，不冒充「需求已满足」。
    # 边界（2026-11-10）：「5 人份」是**软数量**（供 N 人使用的奖品/用品，不严格要求 N 件，
    # 一件桌游就能供 5 人玩）→ 凑不足只给信息性提示，不判不可行；
    # 「12 把键盘 / 28 件外套」是硬数量（件数即交付物）→ 短缺必须判不可行。
    quantity_shortfall = 0
    qty_is_headcount = _is_headcount_qty(intent_query or "")
    if requested_qty is not None:
        for plan, pid in ((plan_a, "A"), (plan_b, "B")):
            # 件数口径：普通模式每件 quantity=1，同款×N 模式 quantity=N 的 1 行
            # 就是 N 件 —— len(items) 会把「1 行×32」误判成「缺 31 件」。
            got = sum(int(it.get("quantity") or 1) for it in (plan.get("items") or []))
            if got < requested_qty:
                if qty_is_headcount:
                    plan["quantity_note"] = (
                        f"💡 按「{requested_qty} 人份」理解（供 {requested_qty} 人使用），"
                        f"方案 {pid} 提供 {got} 件即可满足，无需凑满 {requested_qty} 件"
                    )
                    continue
                plan["feasible"] = False
                plan["quantity_shortfall"] = True
                note = (
                    f"⚠️ 需求 {requested_qty} 件，本轮方案 {pid} 仅找到 {got} 件符合约束"
                    f"（预算/禁忌预筛后候选不足），未静默凑数"
                )
                note += _uniform_qty_hint(requested_qty, got, intent_query or "")
                plan["quantity_note"] = note
                quantity_shortfall = max(quantity_shortfall, requested_qty - got)

    # P0-3 最贴题的软预算上限：没有明确预算时，「匹配优先」会系统性偏好长描述奢侈品
    # （关键词多、描述长、命中就多 → 越贵越「贴题」）。给 B 加软上限 = A 总价 × 3：
    # 超出的不静默展示为「推荐」，打标交给前端折叠到「超预算」区。
    soft_cap = None
    if budget_limit is None:
        soft_cap = round(plan_a.get("total", 0) * 3, 2)
        if plan_b["total"] > soft_cap:
            plan_b["soft_cap_exceeded"] = True
            plan_b["soft_cap_note"] = (
                f"总价 ${plan_b['total']:.2f} 超过最省方案 3 倍（${soft_cap:.2f}），"
                "「最贴题」按关键词匹配分排序会系统性偏好长描述的高价商品，已折叠至「超预算」区"
            )
            plan_b["trade_offs"] = list(plan_b.get("trade_offs") or []) + [plan_b["soft_cap_note"]]
    else:
        plan_b["soft_cap_exceeded"] = plan_b["total"] > budget_limit

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
                search_query, retrieval_note, nearest, channel3_status, recalled_count,
            ),
        }

    # 评审盯点②：数量不足 = 需求未满足，顶层同样不能标「可行」。
    # 前端 setInfeasible 只在「feasible=false 且无 plan」时进无解卡；这里 plan 仍在，
    # 前端正常渲染方案卡 + 黄色数量警示条，不冒充「需求已满足」。
    overall_feasible = quantity_shortfall == 0

    return {
        "feasible": overall_feasible,
        "plan_a": plan_a,
        "plan_b": plan_b,
        "source": source,
        # 只有「同池升级」模式才谈替换上限；本地兜底目录是两套独立策略，不存在替换。
        "comparison": _compare_plans(
            plan_a, plan_b, budget_limit,
            max_upgrades=DEFAULT_MAX_UPGRADES if share_basis else None,
        ),
        "retrieval": _retrieval_meta(
            intent_query, source, catalog, filtered,
            search_query, retrieval_note, nearest, channel3_status, recalled_count,
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
    recalled_count: int = 0,
) -> dict[str, Any]:
    """检索证据：用户看到的「推荐依据」就是这份元数据的镜像。

    - query：用户原句（推荐依据回显用，保证「依据」就是用户的话）
    - search_query：实际发给 Channel3 的检索词（中英混检；诚实呈现，不假装检索词=原句）
    - source：channel3 / k1_catalog
    - candidate_count / matched_count / filtered_count：检索漏斗三数字，**口径单一来源**
      （2026-11-14 修正，评审可心算核对）：
        candidate_count = 原始召回条数（Channel3/本地目录返回数）
        matched_count   = 通过约束预筛后的最终池条数
        filtered_count  = candidate_count − matched_count（**由后端强制保证**，绝不出现
          「候选 19 · 命中 19 · 过滤 1」这类 sum 不守恒的矛盾）
    - note：检索层明明相关商品时的诚实说明（如全量不相干 → 回落本地目录），
      不含数字口径 —— 数字全部由上面三个结构化字段表达
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
    matched_count = len(filtered)
    candidate_count = recalled_count if recalled_count else len(catalog)
    return {
        "query": intent_query or "",
        "search_query": search_query or intent_query or "",
        "source": source,
        "candidate_count": candidate_count,
        "matched_count": matched_count,
        # 强制守恒：过滤数 = 候选 − 命中，永远可心算核对（评审盯点，2026-11-14）。
        "filtered_count": max(0, candidate_count - matched_count),
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


def _select_independent(
    *,
    candidates: list[dict[str, Any]],
    strategy: Literal["cheap", "relevance"],
    relevance: dict[str, dict[str, Any]],
    budget_limit: float | None,
    max_items: int | None,
    min_items: int,
) -> tuple[list[dict[str, Any]], float]:
    """独立选品（不以 A 为底座）：A 按价格从低到高，B 按关键词匹配分从高到低。"""
    sorted_candidates = sorted(
        candidates,
        key=lambda c: (
            float(c.get("price", 0) or 0) if strategy == "cheap"
            else -float(relevance.get(c["id"], {}).get("score", 0))
        ),
    )

    selected: list[dict[str, Any]] = []
    spent = 0.0
    limit = max_items if max_items is not None else len(candidates)

    for item in sorted_candidates:
        price = item.get("price", 0)
        if budget_limit is not None and spent + price > budget_limit:
            continue
        selected.append(item)
        spent += price
        if len(selected) >= limit:
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

    return selected, spent


def _pick_unified_main(
    *,
    candidates: list[dict[str, Any]],
    strategy: Literal["cheap", "relevance"],
    relevance: dict[str, dict[str, Any]],
    budget_limit: float | None,
    unified_qty: int,
    base_item: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """同款×N 主款选择（2026-11-14 新增，触发式「统一/同款/队」批量装配）。

    - A（cheap）：组合预算内单价最低的 SKU 作为主款；
    - B（relevance）：优先在 A 主款(base_item)的同族里找「匹配分严格更高
      + tier ≤ T3 + 单件涨幅 ≤ 50% + 组合预算内」的替代款，找到就换（×N 同量），
      否则与 A 同款同量；无 base_item 时退化为候选池里匹配分最高且在预算内的款。

    返回候选 dict（B 升级时带 _substitution_tier/_substitution_basis），
    预算内装不下 N 件时返回 None（外层走空方案 fallback，不静默减量也不硬塞超预算）。
    """
    price_of = lambda c: float(c.get("price", 0) or 0)

    def _affordable(candidate: dict[str, Any]) -> bool:
        return budget_limit is None or float(unified_qty) * price_of(candidate) <= budget_limit

    # B：先尝试基于 A 主款的同族可辩护升级
    if strategy == "relevance" and base_item is not None:
        base_family = _family_key(base_item)
        base_score = float(relevance.get(base_item.get("id", ""), {}).get("score", 0) or 0)
        base_price = price_of(base_item)
        family_alts = [
            c for c in candidates
            if _family_key(c) == base_family and c.get("id") != base_item.get("id")
        ]
        family_alts.sort(
            key=lambda c: (-float(relevance.get(c["id"], {}).get("score", 0) or 0), price_of(c))
        )
        for alt in family_alts:
            alt_score = float(relevance.get(alt["id"], {}).get("score", 0) or 0)
            # 只有「匹配分严格更高」才升级；同分不换（多花的钱买不到额外关键词证据）。
            if alt_score <= base_score + 1e-9:
                continue
            tier, basis = substitution_tier(alt, base_item)
            if tier > MAX_UPGRADE_TIER:
                continue
            if not price_uplift_ok(price_of(alt), base_price):
                continue
            if not _affordable(alt):
                continue
            alt = dict(alt)
            alt["_substitution_tier"] = tier
            alt["_substitution_basis"] = basis
            return alt
        if _affordable(base_item):
            return dict(base_item)

    # A 或 B 独立：按各自口径排序，取第一个预算内的款
    if strategy == "cheap":
        ordered = sorted(
            candidates,
            key=lambda c: (price_of(c), -float(relevance.get(c["id"], {}).get("score", 0) or 0)),
        )
    else:
        ordered = sorted(
            candidates,
            key=lambda c: (-float(relevance.get(c["id"], {}).get("score", 0) or 0), price_of(c)),
        )
    for c in ordered:
        if _affordable(c):
            return dict(c)
    # 预算内装不下 N 件：不静默减量，也不硬塞超预算方案 —— 交给外层 fallback
    return None


def _assemble_strategy_plan(
    candidates: list[dict[str, Any]],
    strategy: Literal["cheap", "relevance"],
    budget_limit: float | None,
    constraints: list[Constraint],
    max_items: int | None = 8,
    min_items: int = 3,
    intent_query: str = "",
    share_basis: bool = False,
    basis_items: list[dict[str, Any]] | None = None,
    max_upgrades: int | None = DEFAULT_MAX_UPGRADES,
    unified_qty: int | None = None,
    unified_strict: bool = False,
) -> dict[str, Any]:
    """A 按价格排序；B 按关键词证据排序。价格高低不代表耐用或质量。

    share_basis=True 时（Channel3 候选），B 不再另起一篮，而是拿 A 的每一件
    做「同品类升级」：同品类里关键词匹配分**严格更高**的候选替换它；没有更好候选
    就原样保留，且最多替换 max_upgrades 件。这样 diff 视图里「共有商品」是真实存在的，
    两方案的价差也能逐件归因。
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

    selection_mode = "independent"
    if unified_qty is not None:
        # 同款×N：不走「N 个不同 SKU」选品，改为主款 ×N；
        # B 在 A 主款上做同族升级（匹配分更高 + T0-T3 + 涨幅受控），否则同款同量。
        main = _pick_unified_main(
            candidates=candidates,
            strategy=strategy,
            relevance=relevance,
            budget_limit=budget_limit,
            unified_qty=unified_qty,
            base_item=basis_items[0] if (share_basis and basis_items) else None,
        )
        if main is not None:
            selected, spent = ([main], float(unified_qty) * float(main.get("price", 0) or 0))
            selection_mode = "unified_xN"
        elif not unified_strict:
            # 团购弱信号兜底：预算内单款装不下 N 件 → 退回同族多 SKU 凑 N 件
            # （每行 quantity=1），由数量诚实段如实标注凑不足部分。
            selected, spent = _select_independent(
                candidates=candidates,
                strategy=strategy,
                relevance=relevance,
                budget_limit=budget_limit,
                max_items=unified_qty,
                min_items=min(min_items, 1),
            )
            selection_mode = "unified_xN_fallback"
        else:
            # 强信号（统一词）装不下 → 保持严格空语义，由外层 fallback 接管。
            selected, spent = [], 0.0
            selection_mode = "unified_xN"
    elif share_basis and basis_items:
        selection_mode = "substitution"
        selected, spent = _upgrade_basis_items(
            basis_items=basis_items,
            candidates=candidates,
            relevance=relevance,
            budget_limit=budget_limit,
            max_items=max_items,
            min_items=min_items,
            max_upgrades=max_upgrades,
            basis_total=sum(float(it.get("price", 0) or 0) for it in basis_items),
        )
        if [it.get("id") for it in selected] == [it.get("id") for it in basis_items]:
            # 没有任何「可辩护的替换」（T0–T3 + 价格硬门）时，绝不假装升级：
            # B 退化为同一候选池里按匹配分独立重排，并把这个事实写进 payload。
            selection_mode = "relevance_reselect"
            selected, spent = _select_independent(
                candidates=candidates,
                strategy="relevance",
                relevance=relevance,
                budget_limit=budget_limit,
                max_items=max_items,
                min_items=min_items,
            )
    else:
        selected, spent = _select_independent(
            candidates=candidates,
            strategy=strategy,
            relevance=relevance,
            budget_limit=budget_limit,
            max_items=max_items,
            min_items=min_items,
        )

    # 构建 PlanItem 列表
    items = []
    for item in selected:
        source_val = item.get("source", "local_catalog")
        qty = unified_qty if selection_mode == "unified_xN" else int(item.get("quantity") or 1)
        items.append({
            "id": item["id"],
            "name": item["name"],
            "category": item.get("category", ""),
            "price": item.get("price", 0),
            "quantity": qty,
            "subtotal": round(float(item.get("price", 0) or 0) * qty, 2),
            "matched_constraints": [],
            "source": source_val,
            "image_url": item.get("image_url", ""),
            "tags": list(item.get("tags", []) or []),
            "match_score": relevance.get(item["id"], {}).get("score", 0),
            "match_tags": relevance.get(item["id"], {}).get("match_tags", []),
            "rationale": relevance.get(item["id"], {}).get("rationale", "该方案按价格从低到高选择" if strategy == "cheap" else "暂无直接关键词匹配证据"),
            # 替换分级（只有 B 的替换件才有）：tier + 解析依据，UI 一行、README 可复算
            "substitution_tier": item.get("_substitution_tier"),
            "substitution_tier_label": (
                tier_label(item["_substitution_tier"])
                if item.get("_substitution_tier") is not None else None
            ),
            "substitution_basis": item.get("_substitution_basis"),
        })

    total = calc_total(items)

    # 逐件「满足了哪几条」：这里曾长期硬编码 [] —— 前端「共 0 条」就是这么来的。
    for item in items:
        item["matched_constraints"] = [
            c.id for c in constraints
            if c.status == "active" and item_satisfies(c, item)
        ]

    # 组合级 matched/unmet：必须和「约束判定」面板走同一个判定源。
    # 根因（评审必修 2026-11-10）：过去这里用 _check_constraint_match（只看中文字串在
    # tags/name 里的子串匹配，无同义词扩展），而面板走 satisfaction.evaluate（有
    # SYNONYM_KEYWORDS 扩展 + 预算/per_item/eco 规则）。两套计算 → 同一约束上面打 ✅、
    # 下面列进「未满足」，评委一眼看到自相矛盾。
    # 修法：统一取 evaluate() 的 verdict —— 只收 unsat 进 unmet；sat 进 matched；
    # unknown 两边都不收（已在判定里显示成 ?，是「判不了」而非「不满足」）。
    active = [c for c in constraints if c.status == "active"]
    verdicts = evaluate(active, items)["constraints"]
    verdict_status = {v["constraint_id"]: v["status"] for v in verdicts}
    matched = [c.id for c in active if verdict_status.get(c.id) == "sat"]
    unmet = [c.id for c in active if verdict_status.get(c.id) == "unsat"]

    trade_offs = _generate_trade_offs(strategy_name, items, total, budget_limit, unmet, intent_query)
    risks = _generate_risks(strategy_name, total, budget_limit)

    return {
        "plan_id": plan_id,
        "strategy": strategy_name,
        # 选品方式：independent（独立排序）/ substitution（A 的同款升级）/ relevance_reselect（无可辩护替换 → 独立重排）
        "selection_mode": selection_mode,
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
    max_items: int | None,
    min_items: int = 1,
    max_upgrades: int | None = DEFAULT_MAX_UPGRADES,
    basis_total: float = 0.0,
) -> tuple[list[dict[str, Any]], float]:
    """B 在 A 的底座上做「局部升级」：

    - 每个 slot 优先保留 A 的原件；
    - 只有当同品类存在「关键词匹配分**严格更高**」的候选，且加进预算仍装得下时，才换成它；
      同分不换 —— 同分意味着关键词证据一样多，多花的钱买不到任何额外证据；
    - 最多替换 max_upgrades 件，其余槽位保留原件（全盘换货不是升级）；
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
    upgrades_done = 0

    def _score(item: dict) -> float:
        return float(relevance.get(item.get("id", ""), {}).get("score", 0) or 0)

    for base in basis_items:
        if max_items is not None and len(selected) >= max_items:
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
        # 同分的候选之间取更便宜的：同分说明证据一样多，没有理由多花钱。
        alts.sort(key=lambda c: (-_score(c), float(c.get("price", 0) or 0)))

        chosen = base_cand
        # 已经换够 max_upgrades 件就停止替换，其余槽位保留 A 的原件。
        can_upgrade = max_upgrades is None or upgrades_done < max_upgrades
        for alt in (alts if can_upgrade else []):
            alt_score = _score(alt)
            # 只有「匹配分严格更高」才算升级；同分不换。
            # 旧实现在同分时优先换更贵的那件，等于用算法保证 B 的价格单调上升 ——
            # 同分意味着关键词证据一样多，多花的钱什么也没买到（这就是
            # 「B 换了 8 件、8 件全涨、共有 0 件」的根因）。
            if alt_score <= base_score + 1e-9:
                continue
            alt_price = float(alt.get("price", 0) or 0)
            # 分级硬门：只有 T0–T3（同一商品 / 同款异规格 / 同品牌）才允许替换。
            # T4–T5（仅同类型）只配出现在推荐泳道的解释里；T6（仅同大类）永不替换。
            alt_tier, alt_basis = substitution_tier(alt, base_cand)
            if alt_tier > MAX_UPGRADE_TIER:
                continue
            # 价格硬门：单件涨幅 ≤ 50%（Walmart SNAP 规则），整篮涨幅 ≤ 30%
            if not price_uplift_ok(alt_price, base_price):
                continue
            if basis_total > 0 and spent + alt_price > basis_total * (1.0 + MAX_TOTAL_UPLIFT):
                continue
            if budget_limit is not None and spent + alt_price > budget_limit:
                continue
            chosen = dict(alt)
            chosen["_substitution_tier"] = alt_tier
            chosen["_substitution_basis"] = alt_basis
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
        if chosen.get("id") != base_cand.get("id"):
            upgrades_done += 1

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
        verb = "都取" if count > 1 else "取自"
        trades.append(f"{count} 件{verb}候选里最便宜的，均价 ${avg:.2f}、合计 ${total:.2f}")
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


def _relevance_sum(items: list[dict[str, Any]]) -> float:
    """方案的关键词匹配总分（Channel3 match_score 求和；本地目录为 0）。"""
    return round(sum(float(it.get("match_score") or 0) for it in items), 2)


def _pareto_relation(plan_a: dict, plan_b: dict) -> dict[str, Any]:
    """用两个**可核对**的目标函数判断 A/B 是否互不占优。

    目标 1：总价越低越好；目标 2：Channel3 关键词匹配总分越高越好。
      - A 更便宜 且 B 匹配分更高 → 两者都在帕累托前沿上，是真实偏好取舍；
      - 一方在两个维度都不劣 → 另一方属于被支配方案，必须当场说明，不能假装是取舍。
    """
    total_a = float(plan_a.get("total") or 0)
    total_b = float(plan_b.get("total") or 0)
    rel_a = _relevance_sum(plan_a.get("items") or [])
    rel_b = _relevance_sum(plan_b.get("items") or [])

    a_dominates = (
        total_a <= total_b and rel_a >= rel_b and (total_a < total_b or rel_a > rel_b)
    )
    b_dominates = (
        total_b <= total_a and rel_b >= rel_a and (total_b < total_a or rel_b > rel_a)
    )

    if a_dominates or b_dominates:
        winner = "A" if a_dominates else "B"
        return {
            "relation": "a_dominates" if a_dominates else "b_dominates",
            "note": f"方案 {winner} 在总价与匹配分上同时不劣，另一方案被支配，建议直接选 {winner}",
            "total_a": round(total_a, 2),
            "total_b": round(total_b, 2),
            "relevance_a": rel_a,
            "relevance_b": rel_b,
        }
    return {
        "relation": "trade_off",
        "note": "两方案互不占优（A 更便宜 / B 匹配分更高），属于真实取舍，不是谁更差",
        "total_a": round(total_a, 2),
        "total_b": round(total_b, 2),
        "relevance_a": rel_a,
        "relevance_b": rel_b,
    }


def _compare_plans(
    plan_a: dict, plan_b: dict, budget_limit: float | None,
    max_upgrades: int | None = None,
) -> dict:
    """比较两个方案的差异。

    统一思想：两套方案来自同一候选池，因此给出 shared_count / 逐件价差归因，
    而不是两篮无关商品之间的金额对比。
    """
    items_a = plan_a.get("items", [])
    items_b = plan_b.get("items", [])
    ids_b = {it.get("id") for it in items_b}
    shared = [it for it in items_a if it.get("id") in ids_b]
    pareto = _pareto_relation(plan_a, plan_b)
    mode_b = plan_b.get("selection_mode")
    # 逐件升级：B 是 A 的「同款替换」底座（_upgrade_basis_items 保证 slot 一一对应）。
    # 只有 substitution 模式才存在「替换」这回事；relevance_reselect / independent 下
    # 按槽位 zip 配对是无意义的（会出现「包装袋 → 桌游」这种假替换）。
    upgrades = []
    for slot, base in enumerate(items_a if mode_b in (None, "substitution") else []):
        if slot >= len(items_b):
            break
        it = items_b[slot]
        if it.get("id") == base.get("id"):
            continue  # 原样保留，非升级
        upgrades.append(
            {
                "item": base.get("name", ""),
                # 替换成了哪一件：逐件 diff 要说清「换成了什么」，否则只能看到价格跳变
                "to_item": it.get("name", ""),
                "to_id": it.get("id", ""),
                "category": base.get("category", ""),
                "from": base.get("price", 0),
                "to": it.get("price", 0),
                "delta": round(float(it.get("price", 0)) - float(base.get("price", 0)), 2),
                # 替换等级：T0 同一商品 / T1 同款异规格 / T2 同品牌同类型 / T3 同品牌
                "tier": it.get("substitution_tier"),
                "tier_label": it.get("substitution_tier_label"),
            }
        )
    only_a_count = len(items_a) - len(shared)
    only_b_count = len(items_b) - len(shared)

    # 文案必须和 pareto 判定一致：不能一边说「互不占优」，一边在推荐里替用户选 A。
    if mode_b == "unified_xN":
        recommendation = (
            "两方案都是同款×N 批量（A/B 各 1 个主款 SKU）：A 选预算内单价最低主款，"
            "B 在同族里做可辩护升级（匹配分更高 + tier≤T3 + 涨幅受控），"
            "找不到则与 A 同款同量"
        )
    elif mode_b == "unified_xN_fallback":
        recommendation = (
            "两方案都是团购默认同款×N，但预算内单款装不下 N 件："
            "退化为同族多款凑数量（每件各 1），凑不足部分由数量诚实段如实标注"
        )
    elif mode_b == "relevance_reselect":
        recommendation = (
            "B 没有做替换：本次候选池里没有可辩护的同款/同品牌代换（tier ≤ T3 且涨幅受控），"
            "于是 B 改为按匹配分独立重排 —— 两方案交集可能为 0，这是数据结论，不是「升级」"
        )
    elif not upgrades and not only_a_count and not only_b_count:
        recommendation = (
            "两方案件件相同：同品类里没有匹配分更高的候选，B 不会为了「看起来不一样」去换更贵的货"
        )
    elif not upgrades:
        recommendation = (
            f"两方案件数不同（A {len(items_a)} 件 / B {len(items_b)} 件），"
            "但同品类里没有匹配分更高的候选可替换 —— 件数差异来自预算与件数上限，不是「升级」"
        )
    else:
        cheaper = sum(1 for u in upgrades if u["delta"] < 0)
        if cheaper == 0:
            price_note = (
                "这几件都更贵 —— 多出来的钱买的是「更贴你的原话」的关键词证据，"
                "不代表更耐用或更好听"
            )
        elif cheaper == len(upgrades):
            price_note = "这几件都更便宜且匹配分更高 —— 候选池里本来就有更划算的选择"
        else:
            price_note = "替换有涨有降，价差逐件可核对"
        recommendation = (
            f"两方案互不占优：预算紧选 A（最低总价）；更在意贴合原话选 B"
            f"（B 只换了 {len(upgrades)} 件同品类、匹配分更高的商品，"
            f"其余 {len(shared)} 件与 A 相同；{price_note}）"
        )

    return {
        "total_delta": round(plan_b["total"] - plan_a["total"], 2),
        "item_count_a": len(items_a),
        "item_count_b": len(items_b),
        "shared_count": len(shared),
        "only_a_count": only_a_count,
        "only_b_count": only_b_count,
        "upgrades": upgrades,
        "budget_limit": budget_limit,
        # 对比标准：把「凭什么这样比」写进响应，评委可以逐条核对，而不是相信文案。
        "criteria": {
            "domain": "同一候选池：硬约束先过滤，A/B 从同一批商品里选",
            "objective_a": "在预算上限内最小化总价（低价优先）",
            "objective_b": "在预算上限内最大化 Channel3 关键词匹配总分（匹配优先）",
            "same_feasibility": "两方案使用同一 min/max 件数与同一预算上限",
            "evidence": "匹配分 = 标题/标签与检索词的词面重合（match_score / match_tags），不含评分或评论",
            # 替换范围的自我约束：全盘换货不是升级，B 只在少数槽位上换。
            "upgrade_scope": (
                f"B 最多替换 {max_upgrades} 件（同品类 + 匹配分严格更高）；其余槽位保留 A 的原件"
                if max_upgrades is not None
                else "B 对每个槽位独立判断（同品类 + 匹配分严格更高才替换）"
            ),
            # 替代分级的硬门：这是「凭什么可以换」的可复算依据
            "substitution_gate": (
                "只有 tier ≤ T3（同一商品 / 同款异规格 / 同品牌）才允许替换；"
                "单件涨幅 ≤ 50%、整篮涨幅 ≤ 30%；解析不出品牌或类型时视为 T6，不替换"
            ),
            "selection_mode_b": mode_b or "independent",
        },
        "metrics": {
            "total_a": pareto["total_a"],
            "total_b": pareto["total_b"],
            "relevance_a": pareto["relevance_a"],
            "relevance_b": pareto["relevance_b"],
            "item_count_a": len(items_a),
            "item_count_b": len(items_b),
            "shared_count": len(shared),
        },
        "pareto": pareto,
        "recommendation": recommendation,
    }


def prefilter_by_constraints(
    catalog: list[dict[str, Any]],
    constraints: list[Constraint],
    *,
    semantic_pool: bool = False,
    empty_fallback_to_base: bool = False,
) -> list[dict[str, Any]]:
    """C1：基于硬约束前置过滤商品目录。

    `semantic_pool=True` 表示候选池本身已经由语义检索得到（Channel3）：
    这时 functional 关键词只用于**收敛**，不用于**清空**。

    `empty_fallback_to_base=True`（本地目录兜底时）：功能关键词 0 命中也只收敛、
    不清空 —— 目录没有「键盘/牛仔裤/围巾」不代表需求无解，多半是 K1 示例目录
    没覆盖该品类。退回「预算/禁忌」首遍池诚实展示，由 satisfaction 判 ❔、ranker 排序。

    为什么（2026-11-10 实测）：Channel3 返回的是英文商品（"Wireless Earbuds"），
    中文约束「耳机」照样匹配不上，硬过滤会把整池清空 —— 等于把"检索成功"
    变成"无解"，然后把用户推到本地社团目录（大衣/耳机都没有）。所有不合格
    判定交给 satisfaction 逐条标 ❔、由 ranker 按相关度排序，这比空手而归诚实。
    """
    if not catalog:
        return []

    hard_constraints = [c for c in constraints if c.status == "active" and c.hard]
    if not hard_constraints:
        return list(catalog)

    # Quantity-type functional constraints (e.g. "5 人份") describe cart-level needs, not
    # item attributes. Filtering items by them empties the whole catalog. Handle them at
    # plan-assembly time instead.
    functional_constraints = [c for c in hard_constraints if c.type == "functional"]
    # Quantity-type constraints may be phrased many ways by the LLM extractor
    # ("5 人份", "社团 5 人", "need 5 sets", "5-piece"). Treat any functional
    # constraint carrying a number as cart-level quantity info.
    quantity_only = all(
        _is_quantity_constraint(c)
        for c in functional_constraints
    ) if functional_constraints else False

    def _blob(item: dict[str, Any]) -> str:
        item_blob = " ".join(str(item.get(k, "")) for k in ("name", "description", "category")).lower()
        tags_blob = " ".join(str(t).lower() for t in (item.get("tags", []) or []))
        return f"{item_blob} {tags_blob}"

    # 第一遍：预算（数字）与禁忌（确证词）—— 这两类是可确证的，任何候选池都照筛
    kept: list[dict[str, Any]] = []
    for item in catalog:
        full_blob = _blob(item)
        passed = True
        for c in hard_constraints:
            if c.type == "budget":
                if c.numeric_value is not None and item.get("price", 0) > c.numeric_value:
                    passed = False
                    break
            elif c.type == "taboo":
                keywords = _extract_keywords(c.content)
                if keywords and any(kw in full_blob for kw in keywords):
                    passed = False
                    break
        if passed:
            kept.append(item)

    if quantity_only or not functional_constraints:
        return kept

    # 第二遍：functional 关键词 —— 命中就收敛；全池都不命中时看候选池性质
    for c in functional_constraints:
        keywords = _extract_keywords(c.content) or [k.strip() for k in c.content.split("、") if k.strip()]
        if not keywords:
            continue
        hits = [item for item in kept if any(kw in _blob(item) for kw in keywords)]
        if hits:
            kept = hits
        elif not semantic_pool and not empty_fallback_to_base:
            return []
        # semantic_pool 或 empty_fallback_to_base 且 0 命中：不做硬筛（中英表述差异 /
        # 描述缺字段 / K1 目录没覆盖该品类），交 satisfaction 判 ❔、ranker 按相关度排序。

    # P1-① 品类硬过滤：需求是**成品**（包/大衣/耳机…）时，材料/半成品（布料/防尘布…
    # 「Cotton Duck Canvas Fabric」不是包，「Heavy Duty Canvas Drop Cloth」也不是）
    # 即使关键词命中也不能进方案。category/name 落在材料市场词 → 剔除。
    kept = _drop_raw_material_items(kept, functional_constraints)
    return kept


# 成品需求 → 材料市场词黑名单：Category3 商品库混着布料商（by the yard / fabric）
# 和成品商（bags / coats）。关键词匹配会把「canvas」「cotton」当成命中，
# 于是布料混进包、防尘布混进帆布包 —— 这是品类污染，必须在 prefilter 硬过滤。
_FINISHED_PRODUCT_HINTS = ("包", "大衣", "外套", "耳机", "音箱", "鞋", "帽", "杯", "瓶", "壶", "桌", "椅", "架", "柜", "灯", "手表", "眼镜", "书", "本", "笔", "伞", "垫", "毯", "枕", "被", "帐篷", "睡袋", "毛巾", "餐具", "碗", "盘")
_RAW_MATERIAL_MARKERS = (
    "fabric", "by the yard", "per yard", "cloth", "raw material", "textile",
    "drop cloth", "canvas panel", "canvas sheet", "upholstery material",
    "yarn", "thread", "sewing material", "craft fabric", "material roll",
)


def _drop_raw_material_items(kept: list[dict[str, Any]], functional_constraints: list[Constraint]) -> list[dict[str, Any]]:
    """成品需求下剔除材料/半成品商品；材料需求（如「买帆布」）不误杀。"""
    if not kept or not functional_constraints:
        return kept
    want_material = False
    for c in functional_constraints:
        content = (c.content or "").lower()
        # 「买帆布/布料/面料」这类本来就要求材料的需求，不做品类剔除
        if any(w in content for w in ("布料", "面料", "帆布面料", "布匹", "裁剪", "手工材料")):
            want_material = True
    if want_material:
        return kept
    is_finished_product = any(
        any(h in (c.content or "") for h in _FINISHED_PRODUCT_HINTS)
        for c in functional_constraints
    )
    if not is_finished_product:
        return kept

    def _is_raw_material(item: dict[str, Any]) -> bool:
        blob = " ".join(
            str(item.get(k, "") or "") for k in ("name", "title", "category", "description")
        ).lower()
        tags = item.get("tags") or []
        if isinstance(tags, (list, tuple)):
            blob = f"{blob} {' '.join(str(t).lower() for t in tags)}"
        return any(m in blob for m in _RAW_MATERIAL_MARKERS)

    clean, dirty = [], []
    for it in kept:
        (dirty if _is_raw_material(it) else clean).append(it)
    # 全池都是材料商品时不下杀手（避免把有效候选清空导致无解），交给判定层当 ❔
    if not clean:
        return kept
    if dirty:
        logger.info("品类硬过滤：剔除 %d 件材料/半成品（成品需求）", len(dirty))
    return clean


def _uniform_qty_hint(requested_qty: int, got: int, intent: str) -> str:
    """「统一/同款/队」意图下数量不足时的下单方式提示。

    2026-11-13 实测（团建统一队鞋）：需求 32 双、候选 14 个不同 SKU 时，方案给
    「14 件不同款式各 1 件」——即使凑满 32 也是 32 个不同款式，仍然不是「统一」队鞋。
    指出正确的下单方式：同一 SKU 下单 N 件。
    """
    if not any(w in (intent or "") for w in ("统一", "一致", "同款", "队")):
        return ""
    label = "统一" if "统一" in intent else "同款"
    tail = "队" if "队" in intent else "采购"
    return (
        f"；「{label}」需求建议从命中商品中选同一 SKU 下单 {requested_qty} 件"
        f"（当前 {got} 件为不同款式各 1 件，不能作为统一{tail}）"
    )


def _is_quantity_constraint(c: Constraint) -> bool:
    """功能约束是否为数量/人数类（购物车层面信息，不应过滤单品）。"""
    text = (c.content or "").lower()
    if c.numeric_value is not None and c.numeric_value <= 100:
        return True
    return any(
        kw in text for kw in ("人份", "person", "people", "qty", "quantity", "份", "人数", "个", "人", "set")
    )


# 数量词解析：用户说「一件大衣」→ 方案只给 1 件，而不是塞 8 件。
# 只解析「明确数量」才 cap；没提数量的（团购场景「买5个人团体的体育用品」靠 min_items 撑展示）不动。
_CJK_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_QTY_UNITS = ("件", "个", "条", "副", "双", "支", "台", "套", "份", "顶", "只", "瓶", "包", "盒", "把", "块", "张", "本", "盏")


def _parse_requested_quantity(text: str) -> int | None:
    """从用户原句解析期望件数；解析不出来返回 None（不强行 cap）。"""
    if not text:
        return None
    # 「一件 / 一个 / 一条 / 一副 / 一双 / 一支 / 一台 / 一套 / 一顶 / 一箱」
    for unit in _QTY_UNITS:
        if f"一{unit}" in text:
            return 1
    # 「N件 / N个 / N条 / 5 人份 / 10套」阿拉伯数字
    m = re.search(r"(\d{1,2})\s*(" + "|".join(_QTY_UNITS) + ")", text)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 50 else None
    # 「两件 / 三支 / 五台」中文数字
    m = re.search(r"([一二两三四五六七八九十])\s*(" + "|".join(_QTY_UNITS) + ")", text)
    if m:
        return _CJK_NUM.get(m.group(1))
    m = re.search(r"(十|二十|三十)\s*(" + "|".join(_QTY_UNITS) + ")", text)
    if m:
        return _CJK_NUM.get(m.group(1))
    # 「5 人份 / 社团 5 人」团购
    m = re.search(r"(\d{1,2})\s*人", text)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 50 else None
    return None


# 软数量判定：N 人份 / N 人 = 「供 N 人使用」，不是「严格买 N 件」。
# 「5 人份的环保桌游奖品」买 1-3 件都算满足（供 5 人玩）；「12 把键盘」是硬数量，
# 缺一件都是没买够。两者对「短缺是否判不可行」的语义完全不同（评审盯点②的边界）。
_HEADCOUNT_RE = re.compile(r"\d{1,2}\s*(?:人份|人)")


def _is_headcount_qty(text: str) -> bool:
    """数量是「人数/人份」类（软数量）：凑不足不判不可行，只给数量参考。"""
    return bool(text and _HEADCOUNT_RE.search(text))


# 「同款×N」触发的统一词 —— 与 _uniform_qty_hint 同一组词，保证提示与实现口径一致。
_UNIFORM_INTENT_WORDS = ("统一", "一致", "同款", "队")


def _unified_batch_qty(text: str) -> int | None:
    """统一/同款/队意图 + 明确硬数量 → 返回 N（触发同款×N 装配）；否则 None。

    边界（与 _is_headcount_qty 分工）：
      - 「32 双统一队鞋」= 硬数量 → 走同款×N ✓
      - 「5 人份环保桌游」含人份（软数量）→ 不同款也满足，不触发 ✓
      - 没提数量（「团建统一配备」）→ None，维持默认选品
    """
    if not text:
        return None
    if not any(w in text for w in _UNIFORM_INTENT_WORDS):
        return None
    if _is_headcount_qty(text):
        return None
    return _parse_requested_quantity(text)
# 团购语境弱信号（2026-11-14 评审 P1-②）：命中这些词 + 明确数量 N>1 + 无反意词，
# 就给「默认同款×N」——修复「团体采购 40 只马克杯 → 5 个不同款各 1 件」的语义落差。
# 与 _UNIFORM_INTENT_WORDS（强信号）互补：强信号=用户明确说统一/同款/队；
# 弱信号=团购语境下「默认相同」，除非用户明确要多样式（_VARIETY_WORDS）。
_GROUP_CTX_WORDS = ("团体", "团购", "一批", "人手一", "每人一", "统一配发", "都买一样的", "批量采购")
_VARIETY_WORDS = ("多种", "不同款式", "不同款", "各一款", "各一件", "不同样式", "各式各样", "多样", "几款", "数款", "选几款")


def _group_unified_qty(text: str) -> int | None:
    """团购弱信号下的「默认同款×N」判定：

    - 命中团购语境词（团体/团购/一批/人手一…）；
    - 明确数量 N 且 N > 1（单件不适用）；
    - 不含反意词（多款/不同款式/各式…）——用户明确要多样式时退回多 SKU；
    - 非人份软数量（每人一份不是「N 件同款」）。

    全部满足才返回 N；否则 None（维持原多 SKU 挑款行为）。
    """
    if not text:
        return None
    if not any(w in text for w in _GROUP_CTX_WORDS):
        return None
    if any(w in text for w in _VARIETY_WORDS):
        return None
    if _is_headcount_qty(text):
        return None
    qty = _parse_requested_quantity(text)
    if qty is None or qty <= 1:
        return None
    return qty