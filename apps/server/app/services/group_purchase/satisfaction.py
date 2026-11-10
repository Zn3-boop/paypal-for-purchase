"""约束满足度判定 —— 让「✅/❌」有证据、可复算、可追溯。

设计红线（本项目已定死）：
  ✅ 必须**可判定 + 带证据 + 可追溯**；否则它只是装饰，比没有更糟。
  判不了的约束（"要好一点"）绝不假装 ✅，一律 `unknown` 并给出原因。

三档判定能力（诚实分级）：
  完全可判：budget（数字比较）/ functional（名称·品类·标签关键词）/ taboo（禁忌词）
  半可判（依赖关键词字典）：eco（环保标签）/ material（材质词）
  不可判：主观质量（"高级感""质感好"）→ unknown

本模块是**纯函数**：不读会话、不写审计、不改 schema。输入约束 + 商品，输出判定。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

STATUS_SAT = "sat"
STATUS_UNSAT = "unsat"
STATUS_UNKNOWN = "unknown"

# 环保语义：命中任一即认为该约束是"可判的环保约束"
ECO_CONTENT_HINTS = ("环保", "eco", "绿色", "可持续", "sustainable", "recycled", "再生", "有机", "organic")
# 商品侧环保证据词：只收「明确环保主张」，不收 cotton/帆布 这类中性材质 ——
# 否则「普通棉T恤」会被判成 ✅（那就是撒谎）。
ECO_ITEM_EVIDENCE = (
    "环保", "eco", "recycled", "再生", "有机", "organic", "bamboo", "竹",
    "可降解", "sustainable", "rpet", "fsc",
)
# 材质词 → 商品侧可核对的证据词（K1 catalog: 不锈钢/塑料/帆布/木质/铝）
MATERIAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "不锈钢": ("不锈钢", "stainless"),
    "塑料": ("塑料", "plastic", "pp", "abs"),
    "帆布": ("帆布", "canvas"),
    "木质": ("木质", "木", "wood", "bamboo"),
    "铝": ("铝", "aluminum", "aluminium"),
    "玻璃": ("玻璃", "glass"),
    "纸": ("纸", "paper", "再生纸"),
}
# 主观质量词：没有评分/口碑字段可核对 → 一律 unknown
SUBJECTIVE_HINTS = (
    "好一点", "更好", "高级", "质感", "好看", "精致", "有档次", "上档次",
    "耐用性更好", "靠谱", "体面", "拿得出手", "有面子",
)

_PREFIXES = ("不要", "别要", "不能有", "不能", "别", "必须", "需要", "想要", "最好", "要", "有")
_STOP = {
    "的", "和", "或者", "或", "以及", "跟", "与", "一点", "一些", "东西", "商品",
    "预算", "美元", "美金", "元", "块", "usd", "$", "以内", "以下", "左右",
}


def _money(value: float) -> str:
    return f"${float(value):.2f}"


def _text_of(item: Any) -> str:
    """商品的可核对文本：名称 + 品类 + 标签（小写）。"""
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    name = get("name", "") or ""
    category = get("category", "") or ""
    tags = get("tags", []) or []
    blob = " ".join([str(name), str(category), *[str(t) for t in tags]])
    return blob.lower()


def _total(items: list[Any]) -> float:
    total = 0.0
    for it in items:
        get = it.get if isinstance(it, dict) else lambda k, d=None: getattr(it, k, d)
        price = float(get("price", 0) or 0)
        quantity = int(get("quantity", 1) or 1)
        total += price * quantity
    return round(total, 2)


def _keywords(content: str) -> list[str]:
    """把约束原文拆成可比对的关键词（剥掉"要/不要/必须"等前缀词）。"""
    text = (content or "").strip()
    if not text:
        return []
    parts = re.split(r"[，。,.、；;：:\s（）()【】\[\]\"'“”‘’]+", text)
    out: list[str] = []
    for part in parts:
        for seg in re.split(r"和|与|跟|以及", part.strip()):
            seg = seg.strip()
            if not seg:
                continue
            for prefix in _PREFIXES:
                if seg.startswith(prefix) and len(seg) > len(prefix):
                    seg = seg[len(prefix):]
                    break
            seg = seg.strip()
            if not seg or seg in _STOP or seg.isdigit() or seg in out:
                continue
            out.append(seg)
    return out


def _get(obj: Any):
    """统一 dict / dataclass 取值。"""
    if isinstance(obj, dict):
        return lambda key, default=None: obj.get(key, default)
    return lambda key, default=None: getattr(obj, key, default)


def _name_of(item: Any, fallback: str = "") -> str:
    return str(_get(item)("name", "") or fallback)


def _judge_constraint(constraint: Any, items: list[Any], budget_limit: float | None) -> dict:
    """单条约束的组合级判定：sat / unsat / unknown + 证据或原因。

    这是「整体满足 N/M 条」与逐条 ✅/❌/❔ 的唯一数据源。
    """
    get = _get(constraint)
    ctype = get("type", "functional") or "functional"
    content = str(get("content", "") or "")
    base = {
        "constraint_id": get("id", "") or "",
        "type": ctype,
        "content": content,
        "source": get("source", "") or "",
        "hard": bool(get("hard", False)),
    }

    # ── 预算：数字比较，组合级唯一主语 ──────────────────────────────
    if ctype == "budget":
        limit = float(get("numeric_value") or budget_limit or 0)
        if not limit:
            return {**base, "status": STATUS_UNKNOWN, "evidence": None,
                    "reason": "预算约束没有可比较的金额，无法判定"}
        total = _total(items)
        if total <= limit:
            return {**base, "status": STATUS_SAT,
                    "evidence": f"{_money(total)} ≤ {_money(limit)}", "reason": None}
        return {**base, "status": STATUS_UNSAT,
                "evidence": f"{_money(total)} > {_money(limit)}（超支 {_money(total - limit)}）",
                "reason": "组合总价超过预算上限"}

    # ── 主观质量：无评分/口碑字段 → 绝不假装 ✅ ──────────────────────
    if any(hint in content for hint in SUBJECTIVE_HINTS):
        return {**base, "status": STATUS_UNKNOWN, "evidence": None,
                "reason": "主观质量无法从商品字段判定（缺评分/口碑数据），请人工确认"}

    keywords = _keywords(content)
    if not keywords:
        return {**base, "status": STATUS_UNKNOWN, "evidence": None,
                "reason": "没有可比对的关键词"}

    # ── 禁忌：任一商品命中禁忌词 → 组合不满足 ────────────────────────
    if ctype == "taboo":
        for kw in keywords:
            for it in items:
                if kw in _text_of(it):
                    return {**base, "status": STATUS_UNSAT,
                            "evidence": f"「{_name_of(it, kw)}」命中禁忌词「{kw}」",
                            "reason": "组合里出现了明确被禁止的商品"}
        return {**base, "status": STATUS_SAT,
                "evidence": f"未出现禁忌词：{'、'.join(keywords)}", "reason": None}

    # ── 材质：词典命中即判；判不了就 unknown（不猜）────────────────────
    materials = [m for m in MATERIAL_KEYWORDS if m in content]
    if materials:
        first_hit: tuple[Any, str, str] | None = None
        for it in items:
            blob = _text_of(it)
            for mat in materials:
                hit_word = next((w for w in MATERIAL_KEYWORDS[mat] if w in blob), None)
                if hit_word:
                    first_hit = (it, mat, hit_word)
                    break
            if first_hit:
                break
        if first_hit:
            it, mat, word = first_hit
            return {**base, "status": STATUS_SAT,
                    "evidence": f"「{_name_of(it, word)}」材质命中「{mat}」（{word}）",
                    "reason": None}
        return {**base, "status": STATUS_UNSAT,
                "evidence": f"组合中没有任何商品命中材质「{'、'.join(materials)}」",
                "reason": "材质约束未满足"}

    # ── 环保：Channel3 没有 eco_label，靠标签词典；无证据即无证据 ──────
    if any(hint in content.lower() for hint in ECO_CONTENT_HINTS):
        for it in items:
            blob = _text_of(it)
            hit_word = next((w for w in ECO_ITEM_EVIDENCE if w in blob), None)
            if hit_word:
                return {**base, "status": STATUS_SAT,
                        "evidence": f"「{_name_of(it, hit_word)}」标签命中环保（{hit_word}）",
                        "reason": None}
        return {**base, "status": STATUS_UNSAT,
                "evidence": "没有任何商品带环保标识（recycled / 环保 / 有机 / 竹纤维…）",
                "reason": "环保约束未满足"}

    # ── 功能/品类：名称·品类·标签关键词命中 ───────────────────────────
    if ctype == "functional":
        for kw in keywords:
            for it in items:
                if kw in _text_of(it):
                    return {**base, "status": STATUS_SAT,
                            "evidence": f"「{_name_of(it, kw)}」命中关键词「{kw}」",
                            "reason": None}
        return {**base, "status": STATUS_UNSAT,
                "evidence": f"没有任何商品命中关键词：{'、'.join(keywords)}",
                "reason": "功能约束未满足"}

    # ── 其它类型：无结构化字段可比 → unknown（进「系统理解到的意图」）──
    return {**base, "status": STATUS_UNKNOWN, "evidence": None,
            "reason": f"约束类型「{ctype}」没有可结构化比对的字段"}
def item_satisfies(constraint: Any, item: Any) -> bool:
    """单件商品是否满足某条约束（供 planner 回填 PlanItem.matched_constraints）。"""
    rows = _item_verdicts(constraint, [item])
    return bool(rows) and rows[0]["status"] == "pass"



def _item_verdicts(constraint: Any, items: list[Any]) -> list[dict]:
    """单条约束的逐件判定：pass / fail / n/a。

    预算是组合级属性，逐件一律 n/a —— 把「$285 ≤ $350」复制到每件上会让证据贬值。
    """
    get = _get(constraint)
    ctype = get("type", "functional") or "functional"
    content = str(get("content", "") or "")

    if ctype == "budget":
        return [{"status": "n/a", "evidence": "组合级判定"} for _ in items]
    if any(hint in content for hint in SUBJECTIVE_HINTS):
        return [{"status": "n/a", "evidence": "不可判定"} for _ in items]

    keywords = _keywords(content)
    if not keywords:
        return [{"status": "n/a", "evidence": "不可判定"} for _ in items]

    rows: list[dict] = []
    is_eco = any(hint in content.lower() for hint in ECO_CONTENT_HINTS)

    if ctype == "taboo":
        for it in items:
            blob = _text_of(it)
            hit = next((kw for kw in keywords if kw in blob), None)
            rows.append({"status": "fail" if hit else "pass",
                         "evidence": f"命中禁忌词「{hit}」" if hit else "未出现禁忌词"})
        return rows

    for it in items:
        blob = _text_of(it)
        hit_kw = next((kw for kw in keywords if kw in blob), None)
        if hit_kw:
            rows.append({"status": "pass", "evidence": f"标签/名称命中：{hit_kw}"})
            continue
        if is_eco:
            eco_word = next((w for w in ECO_ITEM_EVIDENCE if w in blob), None)
            if eco_word:
                rows.append({"status": "pass", "evidence": f"标签命中：{eco_word}"})
                continue
        rows.append({"status": "fail", "evidence": "未命中该约束关键词"})
    return rows


def evaluate(
    constraints: list[Any],
    items: list[Any],
    *,
    budget_limit: float | None = None,
    max_failures_shown: int = 2,
) -> dict:
    """组合级 + 逐件级的满足度总账（纯计算，无副作用）。

    `max_failures_shown`：❌ 最多显 2 条（满屏红会让人怀疑系统），
    超出的不删除、只标 `visible=False` 并记入 `hidden_failures` —— 数据完整，展示克制。
    """
    active = [c for c in (constraints or []) if (_get(c)("status", "active") or "active") == "active"]
    items = list(items or [])

    verdicts: list[dict] = []
    for c in active:
        verdicts.append(_judge_constraint(c, items, budget_limit))

    passed = sum(1 for v in verdicts if v["status"] == STATUS_SAT)
    failed = sum(1 for v in verdicts if v["status"] == STATUS_UNSAT)
    unknown = sum(1 for v in verdicts if v["status"] == STATUS_UNKNOWN)

    shown_failures = 0
    for v in verdicts:
        if v["status"] == STATUS_UNSAT:
            shown_failures += 1
            v["visible"] = shown_failures <= max_failures_shown
        else:
            v["visible"] = True

    hidden = [v["constraint_id"] for v in verdicts if not v["visible"]]

    item_rows: list[dict] = []
    for idx, it in enumerate(items):
        name = _name_of(it)
        item_id = _get(it)("id", "") or f"item-{idx}"
        results = []
        for c, v in zip(active, verdicts):
            row = _item_verdicts(c, [it])[0]
            if v["status"] == STATUS_UNKNOWN:
                row = {"status": "n/a", "evidence": "不可判定"}
            results.append({
                "constraint_id": v["constraint_id"],
                "label": v["content"],
                "type": v["type"],
                **row,
            })
        item_rows.append({
            "item_id": item_id,
            "name": name,
            "price": round(float(_get(it)("price", 0) or 0), 2),
            "passed": sum(1 for r in results if r["status"] == "pass"),
            "failed": sum(1 for r in results if r["status"] == "fail"),
            "results": results,
        })

    total = len(verdicts)
    headline = f"满足 {passed}/{total} 条" if total else "还没有可判定的约束"
    if unknown:
        headline += f" · {unknown} 条无法判定"

    return {
        "judgeable": bool(total),
        "total": total,
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "headline": headline,
        "max_failures_shown": max_failures_shown,
        "hidden_failures": hidden,
        "constraints": verdicts,
        "items": item_rows,
    }

