"""解析 Prompt 组 —— 约束提取、冲突识别、双方案生成、改单意图解析。

对应执行清单 B/C/E/J 模块：
  B1 主控 System Prompt（三条铁律）
  B2 约束提取（自然语言 → 结构化 JSON）
  B3 冲突识别（输出 conflicts[]）
  B4 约束变更解析（"放宽预算/去掉环保" → 更新状态对象）
  
  C2 方案生成 Prompt（双方案 A/B）
  
  E1 改单意图解析（5 类指令）
  
  J1 兜底逻辑（全方案违反硬约束时的提示）

注：实际 LLM 调用由外层 nodes.py 负责，这里只输出 Prompt 模板和输出 Schema。
"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field

ConstraintType = Literal["budget", "functional", "taboo", "preference"]

# ============================================================
# B1: 主控 System Prompt（三条铁律）
# ============================================================
GROUP_PURCHASE_SYSTEM_PROMPT = """你是「团体采购协理」，负责帮助多人协作完成商品采购。

## 三条铁律（必须严格遵守）

1. **约束优先于检索**：所有商品选择必须先满足硬约束（预算上限、禁忌品类、功能必需），再考虑软性偏好。硬约束违反时直接告知无解，不得强行凑单。

2. **立场中立，无解必须直说**：当约束冲突或预算不足时，明确输出「无法满足 + 二选一建议」，而不是模糊处理或隐藏风险。

3. **AI 无支付发起权**：你只能提供方案和风险提示，最终支付必须由用户点击「确认」按钮触发。禁止在输出中生成任何支付链接或直接诱导付款。

## 工作流程

1. 收集成员约束（按来源分组）
2. 检测冲突并给出二选一建议
3. 基于约束生成双方案（低价优先 vs 耐用优先）
4. 标注每套方案的取舍和风险
5. 等待 HITL 改单指令，循环直到用户确认
6. 用户确认后，输出复核面板（含分摊预览）
"""

# ============================================================
# B2: 约束提取输出 Schema
# ============================================================
class ExtractedConstraint(BaseModel):
    """从自然语言提取的一条结构化约束。"""
    type: ConstraintType = Field(..., description="约束类型：budget/functional/taboo/preference")
    content: str = Field(..., description="原始自然语言描述")
    hard: bool = Field(..., description="是否为硬约束（违反则无法购买）")
    source: str = Field(..., description="提出该约束的成员标识")
    numeric_value: float | None = Field(None, description="如果是预算/数量约束，提取的数值")
    category: str | None = Field(None, description="如果涉及特定品类/属性，提取关键词")
    confidence: float = Field(..., ge=0, le=1, description="提取置信度 [0,1]")


class ConstraintExtractionOutput(BaseModel):
    """约束提取的完整输出。"""
    constraints: list[ExtractedConstraint] = Field(..., description="提取到的约束列表")
    extracted_sources: list[str] = Field(..., description="识别到的成员标识列表")
    ambiguous_parts: list[str] = Field(default_factory=list, description="无法明确提取的模糊部分（需要追问）")


B2_CONSTRAINT_EXTRACTION_PROMPT = """你是一个约束提取器。

输入：多位成员的采购需求文本（可能包含自然语言描述）
输出：结构化约束列表（JSON Schema: ConstraintExtractionOutput）

提取规则：
- budget 约束：总金额、单价上限、人均预算等
- functional 约束：必需功能（如"必须有环保认证"、"必须防水"）
- taboo 约束：禁忌品类（如"不要塑料"、"拒绝一次性用品"）
- preference 约束：软性偏好（如"喜欢红色"、"倾向大牌"）
- hard=true 当且仅当违反该约束会导致交易不可行（预算/禁忌/功能必需）
- source 字段必须标注每条约束的提出者（从上下文中推断成员标识）
- numeric_value 尝试从文本中提取数字（如"不超过 1200" -> 1200）

示例：
输入："成员 A：总预算不超过 1200；成员 B：想要环保材质，但不要太贵；成员 C：不要塑料包装"
输出：{{
  "constraints": [
    {{"type": "budget", "content": "总预算不超过 1200", "hard": true, "source": "成员 A", "numeric_value": 1200}},
    {{"type": "functional", "content": "想要环保材质", "hard": false, "source": "成员 B", "category": "环保"}},
    {{"type": "taboo", "content": "不要塑料包装", "hard": true, "source": "成员 C", "category": "塑料"}}
  ],
  "extracted_sources": ["成员 A", "成员 B", "成员 C"],
  "ambiguous_parts": ["不要太贵"]
}}
"""

# ============================================================
# B3: 冲突识别输出 Schema
# ============================================================
class ConflictInfo(BaseModel):
    """一对冲突约束的信息。"""
    constraint_a_id: str = Field(..., description="第一条约束 ID")
    constraint_b_id: str = Field(..., description="第二条约束 ID")
    conflict_type: Literal["budget_mismatch", "functional_taboo", "preference_oppose"] = Field(...)
    severity: Literal["high", "medium", "low"] = Field(..., description="严重程度：high=无法同时满足")
    reason: str = Field(..., description="为何互斥的人类可读说明")
    suggested_resolution: str | None = Field(None, description="建议的解决方式（如二选一）")


class ConflictDetectionOutput(BaseModel):
    """冲突检测的完整输出。"""
    conflicts: list[ConflictInfo] = Field(..., description="检测到的冲突列表")
    summary: str = Field(..., description="冲突情况总览（一句话）")
    blocked_hard_constraints: list[str] | None = Field(None, description="如果存在无法调和的硬冲突，列出涉及的约束 ID")


B3_CONFLICT_DETECTION_PROMPT = """你是一个冲突检测器。

输入：一组已提取的结构化约束（包含来源、类型、数值）
输出：冲突检测结果（JSON Schema: ConflictDetectionOutput）

冲突类型：
- budget_mismatch: 两位成员的预算要求差距过大（如 A 要求≤800，B 要求≥1200）
- functional_taboo: 功能性要求与禁忌冲突（如"要环保材质"但"不要天然材料"）
- preference_oppose: 软性偏好对立（如 A 喜欢红色，B 讨厌红色）

严重度判定：
- high: 两个都是硬约束，无法同时满足 → 必须人工仲裁
- medium: 一个是硬约束，一个是软约束 → 建议优先满足硬约束
- low: 两个都是软约束 → 可权衡取舍

示例：
输入：[{"id": "c1", "type": "budget", "hard": true, "numeric_value": 800, "source": "A"}, 
       {"id": "c2", "type": "budget", "hard": true, "numeric_value": 1200, "source": "B"}]
输出：{{
  "conflicts": [
    {{"constraint_a_id": "c1", "constraint_b_id": "c2", "conflict_type": "budget_mismatch", 
      "severity": "high", 
      "reason": "成员 A 和成员 B 的预算上限差距过大（800 vs 1200，比例 1.5x）",
      "suggested_resolution": "建议二选一：采用较低预算 800，或提高上限至 1200"}},
  ],
  "summary": "检测到 1 个高优先级预算冲突",
  "blocked_hard_constraints": ["c1", "c2"]
}}
"""

# ============================================================
# B4: 约束变更解析输出 Schema
# ============================================================
ConstraintChangeType = Literal["relax_budget", "tighten_budget", "remove_constraint", "add_constraint", "modify_constraint"]


class ConstraintChangeRequest(BaseModel):
    """一条约束变更请求。"""
    change_type: ConstraintChangeType
    target_constraint_id: str | None = Field(None, description="目标约束 ID（add 时为 null）")
    new_value: Any | None = Field(None, description="新值（放松/收紧预算时的新数字）")
    explanation: str = Field(..., description="变更原因/人类可读描述")


class ConstraintChangeParsingOutput(BaseModel):
    """约束变更解析的完整输出。"""
    changes: list[ConstraintChangeRequest] = Field(..., description="解析出的变更请求列表")
    intent_summary: str = Field(..., description="用户意图总结（一句话）")


B4_CHANGE_PARSING_PROMPT = """你是一个约束变更解析器。

输入：用户对约束的自然语言修改指令（如"放宽预算到 1500"、"去掉环保要求"、"增加禁用荧光笔"）
输出：结构化变更请求（JSON Schema: ConstraintChangeParsingOutput）

变更类型：
- relax_budget: 放松预算上限（"放宽/提高到/增加到 XXX"）
- tighten_budget: 收紧预算上限（"降低到/减少到 XXX"）
- remove_constraint: 移除某条约束（"去掉/删除/不用管 XXX 要求"）
- add_constraint: 新增约束（"再加一个 XXX 限制"）
- modify_constraint: 修改约束细节（"把环保改成只要无甲醛"）

示例：
输入："先把预算放宽到 1500，再去掉那个环保材质的要求"
输出：{{
  "changes": [
    {{"change_type": "relax_budget", "new_value": 1500, "explanation": "用户要求将预算上限从原值提升至 1500"}},
    {{"change_type": "remove_constraint", "target_constraint_id": "c_env_001", "explanation": "用户要求移除环保材质约束"}}
  ],
  "intent_summary": "放宽预算至 1500 并移除环保材质约束"
}}
"""

# ============================================================
# C2: 双方案生成输出 Schema
# ============================================================
class PlanItem(BaseModel):
    """方案中的一件商品。"""
    id: str = Field(..., description="商品 ID")
    name: str = Field(..., description="商品名称")
    category: str = Field(..., description="品类")
    price: float = Field(..., ge=0, description="单价")
    quantity: int = Field(..., ge=1, description="数量")
    subtotal: float = Field(..., description="小计")
    matched_constraints: list[str] = Field(default_factory=list, description="匹配的约束 ID 列表")
    rationale: str = Field("", description="选择理由")


class DualPlanOutput(BaseModel):
    """双方案生成的完整输出。"""
    plan_a: dict[str, Any] = Field(..., description="方案 A（低价优先）")
    plan_b: dict[str, Any] = Field(..., description="方案 B（耐用优先）")
    
    # 以下是每个 plan 的共同字段：
    # {
    #   "plan_id": "A"|"B",
    #   "strategy": "低价优先"/"耐用优先",
    #   "items": [PlanItem],
    #   "total": float,
    #   "currency": str,
    #   "matched_constraints": [str],  # 命中的约束 ID
    #   "unmet_constraints": [str],    # 未满足的约束 ID
    #   "trade_offs": [str],           # 本方案的取舍（牺牲了谁的诉求）
    #   "risks": [str],                # 潜在风险
    # }


C2_DUAL_PLAN_GENERATION_PROMPT = """你是一个双方案生成器。

输入：
- 商品目录（带价格/品类/标签）
- 约束列表（含来源、类型、硬/软、数值）
- 冲突检测结论（如有）

输出：两套方案（JSON Schema: DualPlanOutput）

方案策略：
- 方案 A（低价优先）：在满足硬约束的前提下，尽可能降低成本。牺牲：耐用性/品牌/美观度。
- 方案 B（耐用优先）：优先考虑品质/耐用性/口碑。牺牲：成本优化空间。

必填字段：
- items: 商品列表，每项附带 matched_constraints（满足哪些约束）、unmet_constraints（违背哪些约束）
- total: 总价（必须与 items 单价×数量之和对齐）
- trade_offs: 列出本方案牺牲了哪些成员的哪些诉求（如"牺牲了成员 B 的耐用诉求，选择了平价款"）
- risks: 潜在风险（如"奖品材质一般，可能影响活动观感"）

如果所有商品组合都无法满足硬约束，输出空方案并在 risks 中标记「无法满足硬约束」。
"""

# ============================================================
# E1: 改单意图解析输出 Schema
# ============================================================
ModificationType = Literal[
    "swap_item",
    "delete_item",
    "add_item",
    "adjust_budget",
    "adjust_constraint",
    "set_quantity",
]


class CartModificationRequest(BaseModel):
    """一条购物车修改请求。"""
    modification_type: ModificationType
    target_item_id: str | None = Field(None, description="目标商品 ID（add 时为 null）")
    replacement_item_id: str | None = Field(None, description="替换商品 ID（swap 时必填）")
    new_quantity: int | None = Field(None, ge=1, description="新数量（set_quantity 时必填）")
    new_constraint_value: Any | None = Field(None, description="约束调整的新值")
    explanation: str = Field(..., description="修改原因")


class CartModificationOutput(BaseModel):
    """改单意图解析的完整输出。"""
    modifications: list[CartModificationRequest] = Field(..., description="解析出的修改请求列表")
    intent_summary: str = Field(..., description="用户意图总结（一句话）")
    expected_total_delta: float | None = Field(None, description="预计总价变动（可为负数）")


E1_CART_MODIFICATION_PROMPT = """你是一个改单意图解析器。

输入：用户对购物车的自然语言修改指令（如"把横幅换成红色的"、"删掉两瓶水"、"加一个保温杯，预算可以再补 50"）
输出：结构化修改请求（JSON Schema: CartModificationOutput）

修改类型：
- swap_item: 换货（把 X 换成 Y，或用更便宜的 Z 替代）
- delete_item: 删除商品（"删掉/去掉/不要 X"）
- add_item: 新增商品（"加一个/再来一瓶/补一个 X"）
- adjust_budget: 调整预算上限（"预算再加 XX"、"压缩预算到 XX"）
- adjust_constraint: 调整其他约束（"环保要求放宽"、"颜色改成蓝色"）
- set_quantity: 改某件的数量（"HD 300 要 2 个"、"把那件改成 3 份"）

"只要 X / 只需要 X 和 Y / 只要 2 个 Z" 这类「只保留点名商品」的指令，
系统会确定性编译为「对未点名商品逐件 delete_item + 对点名商品 set_quantity」，
不依赖模型理解；模型只需处理其余的改单说法。

示例：
输入："把横幅换成环保材质版本，再删掉两瓶矿泉水"
输出：{{
  "modifications": [
    {{"modification_type": "swap_item", "target_item_id": "item_banner_001", "replacement_item_id": "item_banner_eco", "explanation": "用户要求将普通横幅换成环保材质版本"}},
    {{"modification_type": "delete_item", "target_item_id": "item_water", "new_quantity": 0, "explanation": "用户要求删除两瓶矿泉水"}}
  ],
  "intent_summary": "更换横幅为环保版本并删除两瓶水"
}}
"""

# ============================================================
# J1: 兜底逻辑提示模板
# ============================================================
FALLBACK_TEMPLATES = {
    "hard_constraint_violation": """
⚠️ 当前约束无法满足，无法生成可行方案。

原因：{reason}

建议采取以下任一行动：
1. 删减物料：减少 {suggested_removals}（可节省约 {estimated_savings:.2f} 元）
2. 放宽约束：将「{constraint_to_relax}」的上限调整至 {new_limit:.2f}
3. 增加预算：当前缺口为 {gap:.2f} 元

请明确您的决定，我将重新生成方案。
""".strip(),

    "budget_insufficient": """
⚠️ 预算不足，无法完成采购。

当前预算：{budget:.2f} 元
最低可行方案：{min_total:.2f} 元
缺口：{gap:.2f} 元

建议：
1. 删除 {count} 件非必需商品（{items}）
2. 将预算上限调整为 {new_budget:.2f} 元
""".strip(),

    "no_valid_combination": """
⚠️ 在当前约束条件下，找不到同时满足所有要求的商品组合。

冲突焦点：{conflict_summary}

建议：
1. 解除硬约束「{constraint_id}」
2. 调整预算至 {suggested_budget:.2f} 元
3. 替换部分商品为平价款（已为您找到 {alternatives_count} 个备选）
""".strip(),
}

J1_FALLBACK_PROMPT = """你是一个兜底提示生成器。

输入：
- 当前约束列表及冲突信息
- 预算检查结果
- 可用商品目录

输出：根据具体情况选择合适的兜底模板（FALLBACK_TEMPLATES），填充变量后返回。

原则：
- 不输出任何商品推荐（因为所有推荐都违反硬约束）
- 明确指出无法满足的原因
- 提供可执行的二选一建议
- 语气专业、克制，避免模棱两可
"""