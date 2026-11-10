"""商品替代分级（substitution tiers）—— 领域无关的「能不能换」判定。

行业依据（公开政策，不是自创）：
  - 杂货电商代换顺序：同一商品不同规格 → 同一类型不同品牌 → 同品类最接近项；
  - Walmart 对 SNAP 订单：替代品价格不得高于原商品；
  - schema.org ProductGroup / hasVariant / variesBy：把「同一商品的另一个版本」正式建模。

等级（通用，不绑定某个品类）：
  T0 同一商品            本体相同 + 规格相同
  T1 同款不同规格        本体相同，规格不同（重发/彩胶/不同容量/不同包装数）
  T2 同品牌同类型        品牌相同 + 类型相同 + 规格相同
  T3 同品牌              品牌相同 + 类型相同
  T4 同类型同规格        类型相同 + 规格相同（不同品牌）
  T5 同类型              类型相同（不同品牌/不同规格）
  T6 仅同大类            只有 category 相同 —— **禁止用于替换**

三条硬门（改这里等于改产品规则，README 有对应判定式）：
  1) B 的替换只允许 T0–T3；T4–T5 仅用于推荐泳道的解释；T6 永不出现；
  2) 单件涨幅 ≤ 50%，总额涨幅 ≤ 30%；
  3) 解析不出品牌 / 类型时降级到 T6（拒绝替换）—— 一次错误的「升级」比不升级糟得多。

为什么不用文本相似度：两张不同风格的黑胶标题里都有 vinyl / record，文本相似度会把它们
判成「高度相似」，MMR 的惩罚方向就反了。tier 是领域判定，方向正确。
"""
from __future__ import annotations

import re
from typing import Any

# ---- 硬门 ----
MAX_UPGRADE_TIER = 3      # B 的替换只允许 T0–T3
MAX_RECO_TIER = 5         # 推荐泳道解释可到 T5
NEVER_TIER = 6            # T6 永不出现
MAX_PRICE_UPLIFT = 0.50   # 单件涨幅上限
MAX_TOTAL_UPLIFT = 0.30   # 总额涨幅上限（相对 A 的总价）
MAX_UPGRADES = 3          # 最多替换件数

_TIER_LABELS: dict[int, str] = {
    0: "同一商品",
    1: "同款不同规格",
    2: "同品牌同类型",
    3: "同品牌",
    4: "同类型同规格",
    5: "同类型",
    6: "仅同大类",
}

# ---- 唯一的领域词汇表：类型别名（可扩展；命中不了就退化为 category 相等） ----
_KIND_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("record", ("vinyl", "record", "records", "lp", "lps", "7-inch", "12-inch", "inch single")),
    ("board game", ("board game", "boardgame", "tabletop", "card game", "桌游", "棋盘")),
    ("ball", ("ball", "soccer", "football", "basketball", "volleyball", "球")),
    ("bottle", ("bottle", "flask", "tumbler", "水瓶", "保温瓶")),
    ("headphone", ("headphone", "headphones", "earbud", "earbuds", "耳机")),
    ("book", ("book", "paperback", "hardcover", "书")),
    ("bag", ("bag", "tote", "backpack", "帆布袋", "袋")),
    ("pen", ("pen", "pencil", "marker", "签字笔", "笔")),
    ("shirt", ("shirt", "tee", "t-shirt", "clothing", "大衣", "外套")),
)

_BRAND_TAGS = re.compile(
    r"^\s*(?:brand|artist|maker|manufacturer|publisher|by|作者|品牌)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_BY_PHRASE = re.compile(r"\s+by\s+([^,;/|()\[\]]+)", re.IGNORECASE)
_NOISE_TAGS = re.compile(
    r"(records?|label|released by|turntable lab|world wide stereo|edition|"
    r"release year|remaster|reissue|genre|format|tracks?|vinyl|inch|rpm|single)",
    re.IGNORECASE,
)
_TITLE_SPLIT = re.compile(r"\s+[-\u2013\u2014]\s+")
_PAREN = re.compile(r"[\(\[（【][^\)\]）】]*[\)\]）】]")
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "vol", "volume", "pt", "part",
    "single", "vinyl", "record", "records", "lp", "ep", "inch", "rpm",
    "edition", "special", "original", "new",
})


def _norm(text: Any) -> str:
    if not text:
        return ""
    s = str(text).lower().replace("\u201d", '"').replace("\u201c", '"').replace("\u2019", "'")
    return re.sub(r"\s+", " ", s).strip()


def _tags(item: dict[str, Any]) -> list[str]:
    raw = item.get("tags") or []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return [str(t).strip() for t in raw if str(t).strip()]


def _name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("title") or "").strip()


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]+", _norm(text))
        if len(t) >= 3 and t not in _STOPWORDS
    }


# 规格抽取：容量 / 包装数 / 尺寸（长度）/ 转速 / 尺码 / 人数 / 适龄。
# 都是通用电商字段，不绑定品类：黑胶命中 length+rpm，水瓶命中 capacity，桌游命中 players/age。
_SPEC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("capacity", re.compile(r"\b\d+(?:\.\d+)?\s?(?:ml|cl|l|oz|fl oz|g|kg|lb|lbs)\b")),
    ("pack", re.compile(r"\b\d+\s?(?:pack|pk|pcs|pieces|count|ct|件装)\b")),
    ("length", re.compile(r"(?:^|[^0-9])\d+(?:\.\d+)?\s?(?:inch|\"|\u201d|cm|mm|ft|寸)\b")),
    ("rpm", re.compile(r"\b(?:33|45|78)\s?rpm\b")),
    ("size", re.compile(r"\bsize\s?(?:xxs|xs|s|m|l|xl|xxl|\d+)\b|\b(?:xxs|xl|xxl)\b")),
    ("players", re.compile(r"\b\d+\s?(?:players?|人份|人)\b")),
    ("age", re.compile(r"\b(?:age|ages)\s?\d+\+?\b")),
)


def _canon_spec(raw: str) -> str:
    return re.sub(r"[^a-z0-9.]", "", _norm(raw).replace('"', "inch"))


def extract_spec(item: dict[str, Any]) -> frozenset[str]:
    """规格签名：命中的规格项集合（空集合表示「规格未知」——此时不判「同规格」）。"""
    text = _norm(" ".join([_name(item), *[_norm(t) for t in _tags(item)]]))
    # "7-inch" / "12-inch" / "500-ml"：连字符是排版，不是规格差异，先抹平再匹配
    text = re.sub(r"(\d)\s?[\-\u2010\u2011\u2012\u2013]\s?(?=inch|in\b|cm|mm|ft|ml|oz|kg|lb)", r"\1", text)
    found: set[str] = set()
    for label, pattern in _SPEC_PATTERNS:
        for match in pattern.finditer(text):
            found.add(f"{label}:{_canon_spec(match.group(0))}")
    return frozenset(found)


def extract_brand(item: dict[str, Any]) -> str | None:
    """品牌/厂商/艺人：「Brand: X」类标签优先，其次「... by X」，最后「标题 - 描述」的右半。

    解析错品牌的后果是**更保守**（同品牌不成立 → tier 更低 → 不替换），所以这里宁可少认。
    """
    for tag in _tags(item):
        m = _BRAND_TAGS.match(tag)
        if m:
            return _norm(m.group(1)) or None
    name = _name(item)
    m = _BY_PHRASE.search(name)
    if m:
        return _norm(m.group(1)) or None
    parts = _TITLE_SPLIT.split(name)
    if len(parts) >= 2:
        right = parts[-1].strip()
        if right and not _NOISE_TAGS.search(right) and 0 < len(_tokens(right)) <= 4:
            return _norm(right)
    return None


def extract_kind(item: dict[str, Any]) -> str | None:
    """商品类型：先过别名表（唯一的领域词汇），否则退化为 category 文本。"""
    haystack = _norm(" ".join([_name(item), str(item.get("category") or ""), " ".join(_tags(item))]))
    for kind, needles in _KIND_ALIASES:
        for needle in needles:
            if needle in haystack:
                return kind
    return _norm(item.get("category")) or None


def extract_core_title(item: dict[str, Any]) -> str:
    """商品本体：去掉括号内容和「- 格式/描述」后缀。"""
    name = _PAREN.sub(" ", _name(item))
    parts = _TITLE_SPLIT.split(name)
    if len(parts) >= 2 and _NOISE_TAGS.search(parts[-1]):
        name = " ".join(parts[:-1])
    return _norm(name)


def _same_core(a: str, b: str) -> bool:
    """本体相同：token 集合 Jaccard ≥ 0.8；单 token 标题要求严格相等。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return _norm(a) == _norm(b) and bool(_norm(a))
    if ta == tb:
        return True
    if len(ta) == 1 or len(tb) == 1:
        return ta == tb
    return len(ta & tb) / len(ta | tb) >= 0.8


def substitution_tier(cand: dict[str, Any], base: dict[str, Any]) -> tuple[int, dict[str, str]]:
    """返回 (tier, basis)。basis 是各字段的解析原文，用于 UI 复核与 README 复算。"""
    c_brand, b_brand = extract_brand(cand), extract_brand(base)
    c_kind, b_kind = extract_kind(cand), extract_kind(base)
    c_spec, b_spec = extract_spec(cand), extract_spec(base)
    c_core, b_core = extract_core_title(cand), extract_core_title(base)

    same_brand = bool(c_brand) and c_brand == b_brand
    same_kind = bool(c_kind) and c_kind == b_kind
    # 规格未知时不算「同规格」：宁可不替换，也不猜
    same_spec = bool(c_spec) and c_spec == b_spec

    basis = {
        "brand": f"{b_brand or '?'} → {c_brand or '?'}",
        "kind": f"{b_kind or '?'} → {c_kind or '?'}",
        "spec": f"{','.join(sorted(b_spec)) or '?'} → {','.join(sorted(c_spec)) or '?'}",
        "core": f"{b_core or '?'} → {c_core or '?'}",
    }

    if _same_core(c_core, b_core):
        return (0 if same_spec else 1), basis
    if same_brand and same_kind:
        return (2 if same_spec else 3), basis
    if same_kind:
        return (4 if same_spec else 5), basis
    return NEVER_TIER, basis


def tier_label(tier: int) -> str:
    return _TIER_LABELS.get(tier, "未知")


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """MMR 相似度：由 tier 反推（1 - tier/6），而不是文本相似度。"""
    tier, _ = substitution_tier(a, b)
    return round(1.0 - tier / NEVER_TIER, 4)


def price_uplift_ok(new_price: float, base_price: float) -> bool:
    """单件涨幅上限（Walmart SNAP 规则的同款硬底线）。"""
    if base_price <= 0:
        return new_price <= 0
    return new_price <= base_price * (1.0 + MAX_PRICE_UPLIFT) + 1e-9
