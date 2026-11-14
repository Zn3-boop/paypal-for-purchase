"""Constraints Management APIs - Full Implementation with LLM Extraction.

Single source of truth: session.constraints (in-memory session store).
Conflict detection mirrors ConstraintManager logic but operates on session.constraints.
"""
from __future__ import annotations
import logging
import re
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Literal, List

from app.services.group_purchase.session_state import get_session, update_session
from app.services.group_purchase.constraints import Constraint, BUDGET_CONFLICT_RATIO
from app.services.group_purchase.text_units import CONJUNCTION_RE
from app.services.group_purchase.audit_log import log_constraint_change, log_conflict_detection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/constraints", tags=["constraints"])


class ConstraintCreate(BaseModel):
    type: Literal["budget", "taboo", "preference", "functional"]
    content: str
    source: str
    hard: bool = False
    numeric_value: float | None = None


class ExtractConstraintsRequest(BaseModel):
    text: str
    source: str = "user"


def _detect_conflicts(constraints: list[Constraint]) -> list[dict]:
    """在约束列表上检测冲突（预算差距过大等）。"""
    active = [c for c in constraints if c.status == "active"]
    conflicts = []
    budgets = [c for c in active if c.type == "budget" and c.hard and c.numeric_value]
    for i, a in enumerate(budgets):
        for b in budgets[i + 1:]:
            ratio = max(a.numeric_value, b.numeric_value) / min(a.numeric_value, b.numeric_value)
            if ratio >= BUDGET_CONFLICT_RATIO:
                conflicts.append({
                    "constraint_a": a.to_dict(),
                    "constraint_b": b.to_dict(),
                    "reason": f"预算差距过大：{a.content} vs {b.content}（比例 {ratio:.1f}x）",
                })
    return conflicts


def _group_by_source(constraints: list[Constraint]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for c in constraints:
        grouped.setdefault(c.source, []).append(c.to_dict())
    return grouped


@router.get("")
async def list_constraints(session_id: str = Query(...)):
    """获取当前会话的所有约束及冲突。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # 软删除（status=removed）的约束不再参与看板与下游消费；GET 端即根治过滤。
    constraints = [c for c in session.constraints if c.status == "active"]
    conflicts = _detect_conflicts(constraints)

    return {
        "constraints": [c.to_dict() for c in constraints],
        "conflicts": conflicts,
        "grouped_by_source": _group_by_source(constraints),
    }


@router.post("")
async def add_constraint(session_id: str = Query(...), constraint: ConstraintCreate = None):
    """手动添加一条新约束。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if constraint is None:
        raise HTTPException(status_code=400, detail="Missing constraint body")

    new_constraint = Constraint(
        id=f"c_{session_id}_{len(session.constraints) + 1}",
        type=constraint.type,
        content=constraint.content,
        hard=constraint.hard,
        source=constraint.source,
        numeric_value=constraint.numeric_value,
        status="active",
    )
    session.constraints.append(new_constraint)
    update_session(session)

    conflicts = _detect_conflicts(session.constraints)
    if conflicts:
        log_conflict_detection(
            session_id=session_id,
            user_id=session.user_id or "demo-user",
            actor="constraints_api",
            conflicts=conflicts,
        )

    log_constraint_change(
        session_id=session_id,
        user_id=session.user_id or "demo-user",
        actor="constraints_api",
        constraint_id=new_constraint.id,
        change_type="added",
        new_value=new_constraint.to_dict(),
    )

    return {
        "ok": True,
        "constraint_id": new_constraint.id,
        "conflicts": conflicts,
    }


@router.post("/extract")
async def extract_constraints_from_text(session_id: str = Query(...), req: ExtractConstraintsRequest = None):
    """从自然语言提取结构化约束（LLM 驱动，失败时规则降级）。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    text = req.text if req else ""
    source = req.source if req else "user"
    if not text.strip():
        raise HTTPException(status_code=400, detail="Text is empty")

    extracted = await _extract_with_llm(text, source)

    if not extracted:
        return {"ok": False, "message": "未能提取到任何约束", "extracted_count": 0}

    new_constraints: list[Constraint] = []
    for ec in extracted:
        constraint = Constraint(
            id=f"c_{session_id}_{len(session.constraints) + len(new_constraints) + 1}",
            type=ec["type"],
            content=ec["content"],
            hard=ec["hard"],
            source=ec["source"],
            numeric_value=ec["numeric_value"],
            status="active",
        )
        session.constraints.append(constraint)
        new_constraints.append(constraint)

        log_constraint_change(
            session_id=session_id,
            user_id=session.user_id or "demo-user",
            actor="llm_extractor",
            constraint_id=constraint.id,
            change_type="added",
            new_value=constraint.to_dict(),
        )

    update_session(session)
    conflicts = _detect_conflicts(session.constraints)
    if conflicts:
        log_conflict_detection(
            session_id=session_id,
            user_id=session.user_id or "demo-user",
            actor="constraints_api",
            conflicts=conflicts,
        )

    return {
        "ok": True,
        "extracted_count": len(new_constraints),
        "added_ids": [c.id for c in new_constraints],
        "constraints": [c.to_dict() for c in new_constraints],
        "conflicts": conflicts,
    }


async def _extract_with_llm(text: str, source: str) -> list[dict]:
    """调用 LLM 提取约束；任何失败都降级到规则提取。"""
    try:
        from app.services.llm.client import llm_parse_with_retry

        class ExtractedConstraint(BaseModel):
            constraint_type: Literal["budget", "taboo", "preference", "functional"]
            content: str
            source: str
            hard: bool
            numeric_value: float | None = None
            scope: Literal["total", "per_item"] | None = None

        class ExtractionOutput(BaseModel):
            extracted_constraints: List[ExtractedConstraint]

        prompt = (
            "你是团体采购需求解析器。从下面的自然语言文本中提取结构化约束。\n\n"
            f'文本："{text}"\n\n'
            "约束类型定义：\n"
            "- budget：涉及总金额、预算上限（如\"预算 600\"），hard 一般为 true\n"
            "- taboo：禁忌/排除项（如\"不要塑料\"）\n"
            "- preference：偏好（如\"优先环保\"）\n"
            "- functional：功能性要求（如\"需要 5 人份\"）\n\n"
            "预算作用域（scope）判别（重要）：\n"
            "- 文本说「总预算 / 预算不超过 / 一共不超过 N 元」→ scope=\"total\"（组合总价上限）\n"
            "- 文本说「单件预算 / 每件不超过 / 单个不超过 N 元 / 单价」→ scope=\"per_item\"（每一件都不能超过）\n"
            "- 预算之外的约束 scope 填 null 即可\n\n"
            "数量与用途不是约束：\n"
            "- 「一共采购 12 把 / 5 人份 / 两件」这类数量描述**不要**抽成约束（只提示采购数量）\n"
            "- 「分配给开发工位 / 发给组员日常办公」这类用途背景**不要**抽成约束\n"
            "- 「哑光黑 / 顺滑 / 音质好」这类主观感受词**不要**抽成约束（目录没有评分字段可核对）\n\n"
            f'每条约束的 source 填 "{source}"（除非文本中明确提到别的成员）。\n'
            "如果能从文本提取出数字（金额/人数），填入 numeric_value，否则为 null。\n"
            "只输出 JSON，不要解释。"
        )

        parsed, _ = await llm_parse_with_retry(
            prompt=prompt, resp_model=ExtractionOutput, max_retry=2, timeout=20.0
        )
        if parsed and parsed.extracted_constraints:
            return [
                {
                    "type": ec.constraint_type,
                    "content": ec.content,
                    "source": ec.source or source,
                    "hard": ec.hard,
                    "numeric_value": ec.numeric_value,
                    "scope": ec.scope or "total",
                }
                for ec in parsed.extracted_constraints
            ]
    except Exception as e:
        logger.warning("LLM extraction failed (%s), falling back to rules", e)

    return _simple_rule_extraction(text, source)


def _extract_budget_number(text: str) -> float | None:
    """从一句话里提取「预算金额」，而不是第一个出现的数字。

    "帮我找 5 人份的环保桌游奖品，预算 300 美元" → 300，而不是 5。
    策略：优先取「预算/budget/不超过/以内/上限/控制在/只有」等关键词后方最近的数字；
    协商不到就退而求其次——取紧跟"元/美元/块"前的数字；仍无则判定无预算数字。
    """
    try:
        nums = [
            (m.start(), float(m.group(1).replace(",", "")))
            for m in re.finditer(r"(\d[\d,.]*)", text.replace("，", ","))
        ]
    except (ValueError, AttributeError):
        return None
    if not nums:
        return None
    tl = text.lower()
    best: tuple[int, float] | None = None
    for kw in ("预算", "budget", "不超过", "以内", "上限", "控制在", "只有"):
        for km in re.finditer(re.escape(kw), tl):
            for pos, num in nums:
                # 数字必须紧跟关键词之后（允许 ≤ < = 和空格）
                if km.start() <= pos and pos - km.end() <= 4:
                    if best is None or (pos - km.end()) < best[0]:
                        best = (pos - km.end(), num)
    if best is not None:
        return best[1]
    # 兜底：紧跟货币单位之前的数字（"预算 300" / "300 美元" / "300块"）
    currency = re.search(r"(\d[\d,.]*)\s*(元|美元|美刀|块|dollars?|usd)", tl)
    if currency:
        try:
            return float(currency.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


# ── 规则降级词典：把一句话拆成「原子约束」，而不是整句 ────────────────
# 为什么必须原子化（真实事故，2026-11-10 实测）：
#   planner 的硬过滤是 any(关键词 in 商品文本)。整句当一条约束时，
#   「我需要一件大衣，有设计感，呢绒的，红色，中长款」永远匹配不上任何商品，
#   于是结果只有两种极端：全灭（"找不到可行组合"）或全放行（给用户一堆塑料餐具）。
#   拆成原子后，每条约束都能单独核对，满足度判定才有逐条 ✅/❔/❌ 可谈。
#
# 这是 LLM 不可用（403 / 超时 / 配额）时的兜底路径，词典只覆盖高频词，不追求全量；
# 命中不了的分句仍会落到「品类」兜底，交给 planner 的关键词匹配去碰。
_COLOR_WORDS = (
    # 细分色名放前面：只匹配「黑色」的话，「磨砂黑」这种修饰色会被整段当品类兜底。
    "哑光黑", "磨砂黑", "浅灰", "墨绿", "藏青", "深棕",
    "红色", "蓝色", "黑色", "白色", "灰色", "绿色", "黄色", "紫色", "粉色",
    "棕色", "米色", "卡其色", "焦糖色", "橙色", "银色", "金色",
)
# 与 satisfaction.MATERIAL_KEYWORDS 对齐：判定侧要能核实，抽取侧才敢抽
_MATERIAL_WORDS = (
    "呢绒", "羊毛", "羊绒", "羽绒", "皮革", "真皮", "真丝", "丝绸", "亚麻", "牛仔",
    "棉", "涤纶", "不锈钢", "塑料", "帆布", "木质", "铝", "玻璃", "纸",
)
_STYLE_WORDS = (
    "中长款", "中长", "长款", "短款", "修身", "宽松", "oversize", "连帽", "立领",
    "翻领", "高领", "圆领", "v领", "加大款",
)
# 明确的功能性要求 → 硬约束（用户说"要小巧"，给了大的就是错）
_ATTR_WORDS = (
    "小巧", "迷你", "轻便", "便携", "无线", "蓝牙", "降噪", "防水", "折叠",
    "大容量", "快充", "静音", "保温", "防摔", "抗摔",
    # 20 条模板审计补齐：这些是能从商品标签核对的客观属性，
    # 不收就会整段落到「品类兜底」，判定侧再没 key 可查 → 永远 ❔
    # （或干脆丢失：服4「低跟」、团8「加厚」、生4「保暖」都没抽出来）。
    "抗风", "加固", "耐磨", "编织", "哑光", "磨砂", "人体工学", "宽口",
    "垂感", "软糯", "亲肤", "挺括", "抗皱", "针织", "加厚", "保暖", "宽版", "低跟",
)
# 主观质量词 → 软偏好。K1/Channel3 没有评分/口碑字段可核对，
# 满足度判定会如实给 ❔（缺证据≠违反），绝不假装 ✅。
_SUBJECTIVE_WORDS = (
    "设计感", "音质好", "音质", "质感", "好看", "高级", "精致", "耐用",
    "舒适", "漂亮", "有档次", "口碑好", "性价比",
)
# 用途/场景词不是约束："听音乐"描述了拿来干嘛，不约束商品属性
_USE_CASE_WORDS = (
    "听音乐", "听歌", "送礼", "日用", "日常", "办公", "户外", "运动",
    "学习", "旅行", "家用", "自用", "聚会", "团建",
)
# 用途/场景**分句**的标记词（2026-11-10 全量审计）：
#   「出门应急充电」「写代码打字用」「团建统一配备」「展会物料包」——
# 这些整句描述「拿来干嘛」，商品目录里没有任何字段可核对，留在约束里只会制造 ❔ 噪音
# （20 条模板的 55 条 unknown 里，16 条属于这一类，把「满足 N/M」真实水位淹掉了）。
_USAGE_HINTS = (
    "适合", "方便", "用于", "作为", "当作", "自用", "通勤", "叠穿", "内搭",
    "打字", "打游戏", "走路", "泡茶", "应急", "出门", "放包里", "配备", "福利",
    "物料", "伴手礼", "统一使用", "统一工装", "装扮", "听会议", "传输",
)
# 常见采购品类：兜底时用它把「环保桌游奖品」这类复合词收敛到可检索的名词，
# 否则整段当关键词，商品文本永远匹配不上（目录里写的是"桌游"不是"环保桌游奖品"）。
_CATEGORY_WORDS = (
    "大衣", "外套", "羽绒服", "风衣", "毛衣", "卫衣", "衬衫", "裤子", "裙", "鞋",
    "半身裙", "针织衫", "开衫", "围巾",
    "耳机", "音箱", "音响", "麦克风", "键盘", "鼠标", "显示器", "充电宝", "数据线",
    "桌游", "棋盘", "卡牌", "拼图", "奖品", "礼品", "文具", "笔记本", "签字笔", "笔",
    "水瓶", "水杯", "保温瓶", "保温杯", "杯子", "餐具", "背包", "购物袋", "帆布包", "托特包",
    "雨伞", "玩具",
    "球", "书", "海报", "装饰", "横幅", "徽章", "奖牌", "奖杯",
)
# 「分句里有没有可核对的东西」的统一判据：颜色/材质/版型/功能属性/品类任一命中，
# 就说明这句在描述商品本身（如「装书本电脑」含品类「书」），不能当用途噪音丢掉。
_OBJECT_WORDS = (
    *_COLOR_WORDS, *_MATERIAL_WORDS, *_STYLE_WORDS, *_ATTR_WORDS, *_CATEGORY_WORDS,
)
# 环保类偏好词单独成条（走 satisfaction 的 eco 规则：命中标签算 ✅，缺证据算 ❔）
_ECO_HINTS = ("环保", "绿色", "可持续", "再生", "有机", "eco", "sustainable")
_NEGATION_HINTS = ("不要", "不用", "排除", "禁用", "禁止", "avoid", "no ", "不含")
# 分句开头要剥掉的「说话方式」：我需要 / 买一副 → 大衣 / 耳机
_LEAD_FILLERS = (
    # 「我们团体采购一批针织围巾」→ 针织围巾（2026-11-10 审计：长开头没剥掉时，
    # 整句会落到「品类兜底」变成一条永远判不了的约束）
    "我们团体采购一批", "我们团体采购", "我们团队采购", "我们批量采购", "我们公司采购",
    "我们团体", "我们团队", "团体采购", "批量采购", "我们",
    "我需要", "我想要", "我想买", "我要买", "我要", "帮我找", "帮我买", "帮我",
    "给我", "麻烦", "想买", "来点", "买", "需要", "想要", "要", "有", "求",
)
_MEASURE_WORD_RE = re.compile(r"^(?:一|两|几|\d+)?\s*[件副双套个只张本支把条台盏盒块包袋份枚颗册卷箱罐瓶款部艘对]")


def _strip_lead(seg: str) -> str:
    """剥掉分句开头的说话方式、量词和句尾的「的」。"""
    seg = seg.strip()
    changed = True
    while changed and seg:
        changed = False
        for lead in _LEAD_FILLERS:
            if seg.startswith(lead) and len(seg) > len(lead):
                seg, changed = seg[len(lead):], True
                break
        m = _MEASURE_WORD_RE.match(seg)
        if m and len(seg) > m.end():
            seg, changed = seg[m.end():], True
        if seg.endswith("的") and len(seg) > 2:
            seg, changed = seg[:-1], True
        seg = seg.strip()
    return seg


# 连词切分（2026-11-10 审计 P0-4）：只有**两侧都是内容字符**（汉字/字母/数字）
# 的「和/与/跟/以及」才是连接词。「低跟」「高跟」「鞋跟」里的「跟」是词素
# （鞋跟），右侧是标点/结尾 —— 原来无脑 replace 会把「深棕色，低跟」切成
# 「低」+「跟」两个单字，双双被 len<2 过滤 → 整条约束静默丢失（实测服4）。
# 规则与判定层 satisfaction._keywords 共用一份（text_units.CONJUNCTION_RE），
# 否则修了抽取侧、判定侧还会把「低跟」切成关键词「低」。
def _split_clauses(text: str) -> list[str]:
    """按标点/连词切分成子句。连词仅在两侧都有内容时切（见 CONJUNCTION_RE 注释）。"""
    raw = re.sub(r"[、；;。／/]", "，", text or "")
    raw = CONJUNCTION_RE.sub("，", raw)
    return [p.strip() for p in raw.split("，") if p.strip()]


# Bug 2（评审）：数量描述与用途描述不是约束。
#   「一共采购 12 把」「买 5 人份」「给我发 3 支」→ 数量，由 _parse_requested_quantity 消费
#   「分配给开发工位」「发给组员日常办公」→ 用途背景，进 background 不进判定
# 这两类留在约束列表里会让「满足 N/M」被噪音拉低，且页面出现无法判定的 ❔ 淹没真问题。
_QTY_PATTERN = re.compile(r"(一共|总共|总计|采购|买|发|需要|来)?\s*(\d{1,3}|[一二两三四五六七八九十]+)\s*[件副双套个只张本支把条台盒块包袋份人]")


def _is_qty_or_usage_clause(seg: str) -> bool:
    """是否「数量/用途」分句 —— 是则不进约束（返回 True = 丢弃该分句）。"""
    if not seg:
        return False
    if _QTY_PATTERN.search(seg):
        # 「12 把 / 5 人份 / 一件大衣」—— 注意带品类词的「一件大衣」仍要留（大衣是实体）。
        # 坑 1（评审）：品类判断不能只看中文词表 ——「12 个 Power Bank」「12 副 TWS」的
        # stripped 不含中文类目，若整段判为数量噪音会连品类约束一起丢掉。英文名词兜底。
        stripped = _QTY_PATTERN.sub("", seg).strip()
        if not stripped:
            return True
        has_zh_category = any(w in stripped for w in _CATEGORY_WORDS)
        has_en_word = bool(re.search(r"[A-Za-z]{2,}", stripped))
        return not (has_zh_category or has_en_word)
    if any(kw in seg for kw in ("分配给", "发货给", "发给", "用于", "用到", "给", "在", "工位", "会议", "日常办公", "自用", "送礼")):
        # 用途背景：只有整句都是用途描述才丢弃；「给组员买环保奖品」的「组员」不误伤
        return len(seg) <= 14
    # 用途分句（2026-11-10 审计）：「适合通勤」「出门应急充电」「团建统一配备」
    # 「展会物料包」「作为团队伴手礼」——含用途标记且不含任何可核对属性/品类词 → 丢弃。
    # 「装书本电脑」虽含「用」类语义，但命中品类「书」，是真要求（包要能装书）→ 保留。
    if any(hint in seg for hint in _USAGE_HINTS) and not any(
        w in seg for w in _OBJECT_WORDS
    ):
        return True
    return False


def _collapse_color_hits(hits: list[str]) -> list[str]:
    """颜色命中去重：同一分句里，若一个颜色的「基础色」是另一个的子串
    （棕 ⊂ 深棕、灰 ⊂ 浅灰、绿 ⊂ 墨绿），说明二者描述的是同一种颜色要求，
    只保留最具体的（更长的）那个，避免 满足 N/M 的分子分母一起注水。

    例：「深棕色」→ 命中 深棕+棕色 → 只留 深棕；「浅灰色」→ 只留 浅灰。
    """
    if len(hits) < 2:
        return hits
    bases = {w: (w[:-1] if w.endswith("色") else w) for w in hits}
    return [
        w for w in hits
        if not any(
            w != other and bases[w] in bases[other] and len(bases[w]) < len(bases[other])
            for other in hits
        )
    ]


# 规格/容量：数字+单位（500ml / 1.5L / 316 不锈钢牌号）。
# 值统一归一化成**无空格**形式（"500 ml" → "500ml"）：判定侧 `_keywords` 按空白拆词，
# 留着空格会拆出裸「ml」，反而误命中所有写了容量的商品。
_SPEC_PATTERNS = (
    (re.compile(r"(\d{2,4})\s*(?:ml|毫升)", re.I), "ml"),
    (re.compile(r"(\d(?:\.\d)?)\s*(?:l|升)(?![a-z])", re.I), "l"),
    (re.compile(r"(?:^|[^\d])(316|304|18/8|201)\s*(?:不锈钢|stainless)", re.I), ""),
)


def _extract_specs(seg: str) -> list[str]:
    """抽出可核对的数量规格（容量/钢号）。"""
    specs: list[str] = []
    for pattern, unit in _SPEC_PATTERNS:
        for m in pattern.finditer(seg or ""):
            # 钢号（unit=""）不能只存裸数字「316」：判定侧 `_keywords` 会把纯数字
            # 当噪音过滤（isdigit），于是「316 不锈钢」永远判成「没有可比对的关键词」。
            # 带上材质上下文输出「316不锈钢」，`satisfaction` 的材质分支
            # （MATERIAL_KEYWORDS 里 `不锈钢 in content`）才能对上 stainless。
            raw = f"{m.group(1)}{unit}" if unit else f"{m.group(1)}不锈钢"
            if raw not in specs:
                specs.append(raw)
    return specs


def _classify_clause(seg: str) -> list[dict]:
    """把一个子句拆成若干原子约束（一个子句可能同时表达多个要求）。

    `红色呢绒中长款大衣` → 材质/颜色/款式/品类 四条，这才是用户的真实意思。
    返回空列表表示"这不是约束"（用途词、数量描述、否定分句等）。
    """
    if not seg or len(seg) < 2:
        return []
    # 否定分句交给 taboo 分支（整句记录，planner 自带否定词剥离），
    # 否则「不要塑料」会被这里抽成正面的「塑料材质」，语义直接反了。
    if any(k in seg for k in _NEGATION_HINTS):
        return []
    # 预算分句已由 budget 分支处理
    if any(k in seg for k in ("预算", "budget", "美元", "美金", "usd")):
        return []
    # 数量/用途分句不是约束（Bug 2）：「一共采购 12 把」「分配给开发工位」不进约束列表
    if _is_qty_or_usage_clause(seg):
        return []
    if any(w in seg for w in _USE_CASE_WORDS) and len(seg) <= 5:
        return []

    out: list[dict] = []

    def _add(ctype: str, content: str, hard: bool) -> None:
        if content and all(c["content"] != content for c in out):
            out.append({"type": ctype, "content": content, "hard": hard})

    # 规格数字（2026-11-13 实测）：「316 不锈钢，500ml 便携款」此前只抽出
    # 「不锈钢材质」「便携」——316 和 500ml 直接消失，于是给要 500ml 的用户
    # 推荐 40oz（≈1183ml）的旅行杯还判「满足」。规格是客观要求，必须单独成条：
    # 商品写了就 ✅，目录没写容量就诚实 ❔，而不是静默当没提过。
    for spec in _extract_specs(seg):
        _add("functional", spec, True)

    # 材质/颜色/款式都是"外观属性"，目录常缺字段 → 软偏好：
    # 判定侧命中算 ✅、缺证据算 ❔，绝不因目录没写就判 ❌。
    for word in _MATERIAL_WORDS:
        if word in seg:
            _add("preference", f"{word}材质", False)
    # 颜色去重（2026-11-10 审计）：「深棕色」同时命中「深棕」「棕色」、
    # 「浅灰色」同时命中「浅灰」「灰色」——同一颜色要求计了两遍，把 满足 N/M
    # 的分子和分母一起注水。只保留最具体的那个。
    color_hits = [w for w in _COLOR_WORDS if w in seg]
    for word in _collapse_color_hits(color_hits):
        _add("preference", word, False)
    for word in _STYLE_WORDS:
        if word in seg:
            _add("preference", word, False)
    # 明确的功能性要求 → 硬约束（用户要"小巧"，给了大的就是错）
    for word in _ATTR_WORDS:
        if word in seg:
            _add("functional", word, True)
    # 主观质量词 → 软偏好：没有评分/口碑字段可核对，判定永远是 ❔
    for word in _SUBJECTIVE_WORDS:
        if word in seg:
            _add("preference", word, False)
    # 环保偏好 → 软偏好，走 eco.label_evidence 规则
    for word in _ECO_HINTS:
        if word in seg.lower():
            _add("preference", "环保", False)
            break
    # 品类兜底（只取命中的最长品类词，避免"奖品"和"桌游"各来一条）
    hits = [w for w in _CATEGORY_WORDS if w in seg]
    if hits:
        _add("functional", max(hits, key=len), True)
    elif not out:
        # 词典都没命中：整段当品类，让 planner 的关键词匹配去碰
        _add("functional", seg, True)

    return out


def _dedupe_substrings(items: list[dict]) -> list[dict]:
    """丢弃是别的约束真子串的短词：「中长款」已表达「长款」，「音质好」已表达「音质」。

    taboo 约束存的是整句（planner 靠它自己剥否定词），不参与比较，
    否则整句会把「环保」「桌游」这类原子约束全部当成子串吞掉。
    """
    comparables = [i for i in items if i["type"] != "taboo"]
    kept_ids = {
        id(item)
        for item in comparables
        if not any(
            item["content"] != other["content"] and item["content"] in other["content"]
            for other in comparables
        )
    }
    return [i for i in items if i["type"] == "taboo" or id(i) in kept_ids]


def _simple_rule_extraction(text: str, source: str) -> list[dict]:
    """规则降级提取：预算/禁忌各一条，其余拆成原子约束（见上方词典注释）。

    LLM 可用时不会走到这里；这里是「外部依赖全挂」时最后的诚实底线：
    宁可给出可逐条核对的原子约束（并诚实标 ❔），也不要整句一条谁都对不上。
    """
    extracted: list[dict] = []
    tl = text.lower()

    if any(kw in tl for kw in ("预算", "budget", "不超过", "以内", "上限")):
        num = _extract_budget_number(text)
        if num is not None:
            # Bug 1（评审）：「单件预算不超 130」「每件不超过 N」「单个 N 元」是**单件单价**上限，
            # 不是组合总价上限。scope 记入约束，planner 只把 total 预算当组合 cap。
            per_item = any(kw in tl for kw in ("单件", "每件", "单个", "单价", "per item", "each"))
            extracted.append({
                "type": "budget",
                "content": f"{'单件' if per_item else '总'}预算≤{num}",
                "source": source, "hard": True, "numeric_value": num,
                "scope": "per_item" if per_item else "total",
            })

    if any(kw in tl for kw in ("不要", "排除", "禁用", "avoid", "no ")):
        extracted.append({
            "type": "taboo", "content": text, "source": source,
            "hard": False, "numeric_value": None,
        })

    if any(kw in tl for kw in ("优先", "prefer", "喜欢", "希望", "尽量")):
        extracted.append({
            "type": "preference", "content": text, "source": source,
            "hard": False, "numeric_value": None,
        })

    # 原子约束：每个分句可产出多条，按 content 去重
    seen_contents = {e["content"] for e in extracted}
    for clause in _split_clauses(text):
        seg = _strip_lead(clause)
        for picked in _classify_clause(seg):
            if picked["content"] in seen_contents:
                continue
            seen_contents.add(picked["content"])
            extracted.append({
                "type": picked["type"],
                "content": picked["content"],
                "source": source,
                "hard": picked["hard"],
                "numeric_value": None,
            })

    return _dedupe_substrings(extracted)

    return extracted


@router.delete("/{constraint_id}")
async def delete_constraint(session_id: str = Query(...), constraint_id: str = None):
    """删除一条约束（软删除）。"""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    target = next((c for c in session.constraints if c.id == constraint_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Constraint not found")

    target.status = "removed"
    update_session(session)

    log_constraint_change(
        session_id=session_id,
        user_id=session.user_id or "demo-user",
        actor="constraints_api",
        constraint_id=constraint_id,
        change_type="removed",
    )
    return {"ok": True, "deleted_id": constraint_id}