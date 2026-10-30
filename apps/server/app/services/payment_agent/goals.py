"""目标约束记忆 —— 从"帮你付"升级成"替你守"。

搬自 aits.copy22 双层记忆的"长期目标/约束"层，但在支付场景里做成结构化
约束，不是聊天记录：

  用户说"我在存钱买相机" → 存成 SavingGoal {category, amount}
  每次支付前检查 conflict = 品类冲突 / 金额冲突 / 时间窗冲突

持久化：用户会话级内存（演示），接口保持轻量且可预测。
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 享受型消费品类（与 shopper catalog category 对齐）
# 目标约束只审视"可计划/享受型"消费，且达到显著金额门槛才触发；
# donation（公益）属于利他性支出，不在品类禁令内。
_INDULGENT_CATEGORIES = {
    "music", "dining", "film", "book", "merch", "experience", "fashion",
}
# 触发品类禁令的显著金额门槛（小额零嘴不消耗目标一致性）
CATEGORY_MIN_AMOUNT = 20.0


@dataclass
class SavingGoal:
    """一条结构化储蓄目标。"""

    user_id: str
    target_label: str          # "存钱买相机"
    target_amount: float = 0.0 # 0 = 未指定金额（只约束冲突品类）
    protected_category: str = "savings"  # 任何与目标冲突的消费都会被审视
    monthly_spend_cap: float = 300.0     # 用户月度消费上限（防御性护栏）
    category_min_amount: float = CATEGORY_MIN_AMOUNT
    since: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    active: bool = True

    def conflicts_with(self, *, category: str, amount: float, monthly_spent: float) -> tuple[bool, str]:
        """冲突检测：品类敏感 + 月度总量 + 金额接近目标。"""
        reasons: list[str] = []
        if (
            category in _INDULGENT_CATEGORIES
            and self.protected_category == "savings"
            and amount >= self.category_min_amount
        ):
            reasons.append(
                f"当前「{category}」消费 {amount:.2f} 属于享受型支出，"
                f"与储蓄目标（{self.target_label}）冲突"
            )
        if monthly_spent + amount > self.monthly_spend_cap:
            reasons.append(
                f"本月已消费 {monthly_spent:.2f}，加上这笔 {amount:.2f} "
                f"将超出月度上限 {self.monthly_spend_cap:.2f}"
            )
        if self.target_amount > 0 and amount >= self.target_amount * 0.25:
            reasons.append(
                f"单笔 {amount:.2f} 已占储蓄目标（{self.target_label}，"
                f"{self.target_amount:.2f}）的四分之一"
            )
        return bool(reasons), "；".join(reasons) if reasons else ""


class GoalMemory:
    """进程内目标约束记忆。key = user_id。"""

    def __init__(self) -> None:
        self._store: dict[str, list[SavingGoal]] = {}
        self._lock = threading.Lock()

    def set_goal(self, goal: SavingGoal) -> None:
        with self._lock:
            self._store.setdefault(goal.user_id, []).append(goal)

    def set_goal_from_text(self, user_id: str, text: str) -> SavingGoal | None:
        """从一句自然语言提取目标（规则近似，不做 LLM 调用——提取本身是弱信号）。"""
        m = re.search(r"(\d[\d,.]*\s*[元kK百千万美元]?)", text)
        amount = 0.0
        if m:
            raw = m.group(1).replace(",", "").replace(" ", "")
            try:
                amount = float(re.sub(r"[^\d.]", "", raw))
            except ValueError:
                amount = 0.0
        label = text.strip()[:50]
        goal = SavingGoal(user_id=user_id, target_label=label, target_amount=amount)
        self.set_goal(goal)
        return goal

    def get_goals(self, user_id: str) -> list[SavingGoal]:
        with self._lock:
            return [g for g in self._store.get(user_id, []) if g.active]

    def check_purchase(
        self, user_id: str, *, category: str, amount: float, monthly_spent: float
    ) -> list[str]:
        """返回所有命中该笔消费的目标约束理由（空 = 放行）。"""
        blocked: list[str] = []
        for goal in self.get_goals(user_id):
            hit, reason = goal.conflicts_with(
                category=category, amount=amount, monthly_spent=monthly_spent
            )
            if hit:
                blocked.append(f"【目标:{goal.target_label}】{reason}")
        return blocked


# 全局 registry（演示用）
_registry: dict[str, GoalMemory] = {}
_lock = threading.Lock()


def get_goal_memory(session_id: str | None = None) -> GoalMemory:
    """按 session_id 收敛到同一 GoalMemory 实例（按会话复用内存实例）。"""
    key = session_id or "default"
    with _lock:
        mem = _registry.get(key)
        if mem is None:
            mem = GoalMemory()
            _registry[key] = mem
        return mem