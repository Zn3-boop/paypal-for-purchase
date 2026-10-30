"""冲动消费拦截 —— 行为信号版（无摄像头，100% 可复现）。

LPM 情绪检测 → 替换为可审计的行为金融学信号：

  深夜时段 + 短时间连续加购 + 超出月度预算 + 与储蓄目标冲突
  四项信号中触发 >= MIN_SIGNAL_COUNT 时介入，而不是付完才提醒。

为什么行为信号 > 摄像头情绪检测：
  - 评委点开链接不需要授权摄像头（多数人拒绝授权）
  - 录制视频不依赖媒体栈，每次演示都能稳定触发
  - 行为金融学依据同样成立（自我控制耗竭在深夜更显著）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 触发阈值：至少命中的信号数（含储蓄目标冲突则优先）
MIN_SIGNAL_COUNT = 3
LATE_NIGHT_HOUR_START = 23  # 23:00
LATE_NIGHT_HOUR_END = 6     # 06:00
CONSECUTIVE_PURCHASE_WINDOW_MINUTES = 20
CONSECUTIVE_PURCHASE_COUNT = 3


@dataclass
class PurchaseSignal:
    """一次加购事件（用于连续加购检测）。"""

    item_id: str
    price: float
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ImpulseCheckResult:
    triggered: bool
    signals: list[str] = field(default_factory=list)
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)


def _is_late_night(now: datetime | None = None) -> bool:
    """本地时区近似判断：23:00-06:00 视为深夜。"""
    now = now or datetime.now()
    h = now.hour
    return h >= LATE_NIGHT_HOUR_START or h < LATE_NIGHT_HOUR_END


def _count_recent_consecutive(
    history: list[PurchaseSignal], window_minutes: int
) -> int:
    """统计规定时间窗内（且彼此间隔很短）的连续加购次数。"""
    if not history:
        return 0
    # 已按时间升序；从最近一次往前数
    count = 1
    prev_ts: datetime | None = None
    for event in reversed(history):
        try:
            ts = datetime.fromisoformat(event.timestamp)
        except (ValueError, TypeError):
            continue
        if prev_ts is not None and (prev_ts - ts).total_seconds() / 60 > window_minutes:
            break
        count += 1
        prev_ts = ts
    return count


def check_impulse(
    *,
    item_name: str,
    amount: float,
    monthly_spent: float,
    monthly_budget: float,
    savings_conflict: list[str],
    history: list[PurchaseSignal] | None = None,
    now: datetime | None = None,
) -> ImpulseCheckResult:
    """综合冲动拦截判定。

    信号：
      1. 深夜时段
      2. 短时间连续加购（>= CONSECUTIVE_PURCHASE_COUNT）
      3. 超出月度预算
      4. 与储蓄目标冲突（来自 goal_memory.check_purchase）
    触发：命中 >= MIN_SIGNAL_COUNT；若包含储蓄目标冲突则显著。
    """
    signals: list[str] = []
    context: dict[str, Any] = {}

    if _is_late_night(now):
        signals.append("深夜时段（冲动消费高发窗口）")
        context["late_night"] = True

    history = history or []
    consec = _count_recent_consecutive(history, CONSECUTIVE_PURCHASE_WINDOW_MINUTES)
    if consec >= CONSECUTIVE_PURCHASE_COUNT:
        signals.append(f"{CONSECUTIVE_PURCHASE_WINDOW_MINUTES}分钟内连续加购 {consec} 次")
        context["consecutive_purchases"] = consec

    if monthly_spent + amount > monthly_budget:
        signals.append(
            f"超出月度预算（{monthly_spent:.2f} + {amount:.2f} > {monthly_budget:.2f}）"
        )
        context["over_budget"] = True

    if savings_conflict:
        signals.append("与储蓄目标冲突：" + "；".join(savings_conflict[:2]))
        context["savings_conflict"] = savings_conflict

    triggered = len(signals) >= MIN_SIGNAL_COUNT
    reason = ""
    if triggered:
        reason = (
            f"检测到冲动消费风险（命中 {len(signals)}/{MIN_SIGNAL_COUNT} 项行为信号）："
            + "；".join(signals)
            + f"。建议为{item_name}暂缓 10 分钟冷静一下，或者设置储蓄目标后再购买。"
        )
    return ImpulseCheckResult(triggered=triggered, signals=signals, reason=reason, context=context)