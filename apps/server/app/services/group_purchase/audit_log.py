"""决策审计日志 —— 记录所有采购决策的完整轨迹。

对应执行清单 I 模块：
  I1 日志 schema
  I2 五个埋点（装饰器形式）
  I3 审计台账 UI（前端调用此 API 读取）

持久化：基于文件的 JSONL 格式（每行一条日志），便于查询和分析。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

AuditEventType = Literal[
    "constraint_changed",       
    "plan_generated",           
    "cart_modified",            
    "review_confirmed",         
    "payment_completed",        
    "conflict_detected",        
    "budget_violated",          
    "fallback_triggered",       
    "order_status_changed",     
    "payment_amount_mismatch",
]


@dataclass
class AuditLogEntry:
    """单条审计日志。
    
    I1: 日志 schema 定义
    """
    timestamp: str
    round_num: int
    event: AuditEventType
    session_id: str
    user_id: str
    actor: str
    data: dict[str, Any] = field(default_factory=dict)
    constraint_snapshot: list[dict] | None = None
    plan_snapshot: list[dict] | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "round_num": self.round_num,
            "event": self.event,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "actor": self.actor,
            "data": self.data,
            "constraint_snapshot": self.constraint_snapshot,
            "plan_snapshot": self.plan_snapshot,
        }
    
    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class AuditLogger:
    """会话级审计日志管理器。"""
    
    def __init__(self, store_dir: str = ""):
        self.store_dir = store_dir or os.getenv(
            "AUDIT_LOG_STORE_DIR",
            "./data/audit_logs",
        )
        os.makedirs(self.store_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._log_cache: dict[str, list[AuditLogEntry]] = {}
    
    def _get_log_file_path(self, user_id: str) -> Path:
        user_dir = os.path.join(self.store_dir, user_id)
        os.makedirs(user_dir, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return Path(user_dir) / f"audit_{today}.jsonl"
    
    def log(
        self,
        session_id: str,
        user_id: str,
        actor: str,
        event: AuditEventType,
        data: dict[str, Any] | None = None,
        constraint_snapshot: list[dict] | None = None,
        plan_snapshot: list[dict] | None = None,
        round_num: int | None = None,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            round_num=round_num or 0,
            event=event,
            session_id=session_id,
            user_id=user_id,
            actor=actor,
            data=data or {},
            constraint_snapshot=constraint_snapshot,
            plan_snapshot=plan_snapshot,
        )
        
        with self._lock:
            self._log_cache.setdefault(session_id, []).append(entry)
            
            log_file = self._get_log_file_path(user_id)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry.to_json_line() + "\n")
        
        return entry
    
    def get_logs(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        event_type: AuditEventType | None = None,
        limit: int = 100,
    ) -> list[AuditLogEntry]:
        results: list[AuditLogEntry] = []
        
        with self._lock:
            if session_id and session_id in self._log_cache:
                results = self._log_cache[session_id][-limit:]
            else:
                if user_id:
                    user_dir = os.path.join(self.store_dir, user_id)
                    if os.path.isdir(user_dir):
                        for log_file in sorted(Path(user_dir).glob("audit_*.jsonl")):
                            with open(log_file, "r", encoding="utf-8") as f:
                                for line in f:
                                    try:
                                        data = json.loads(line.strip())
                                        entry = AuditLogEntry(**data)
                                        
                                        if event_type and entry.event != event_type:
                                            continue
                                        results.append(entry)
                                    except json.JSONDecodeError:
                                        continue
        
        results.sort(key=lambda x: x.timestamp, reverse=True)
        return results[:limit]
    
    def get_session_timeline(self, session_id: str) -> list[AuditLogEntry]:
        return self.get_logs(session_id=session_id, limit=200)
    
    def clear_session_cache(self, session_id: str) -> None:
        with self._lock:
            self._log_cache.pop(session_id, None)


_global_logger: AuditLogger | None = None
_logger_lock = threading.Lock()


def get_audit_logger() -> AuditLogger:
    global _global_logger
    if _global_logger is None:
        with _logger_lock:
            if _global_logger is None:
                _global_logger = AuditLogger()
    return _global_logger


# ============================================================
# 便捷函数（五个埋点）
# ============================================================

def log_constraint_change(
    session_id: str,
    user_id: str,
    actor: str,
    change_type: str,
    constraint_id: str | None = None,
    old_value: dict | None = None,
    new_value: dict | None = None,
    all_constraints: list[dict] | None = None,
) -> AuditLogEntry:
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="constraint_changed",
        data={
            "change_type": change_type,
            "constraint_id": constraint_id,
            "old_value": old_value,
            "new_value": new_value,
        },
        constraint_snapshot=all_constraints,
    )


def log_plan_generation(
    session_id: str,
    user_id: str,
    actor: str,
    plan_id: str,
    items: list[dict],
    total: float,
    trade_offs: list[str] | None = None,
    risks: list[str] | None = None,
    all_constraints: list[dict] | None = None,
) -> AuditLogEntry:
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="plan_generated",
        data={
            "plan_id": plan_id,
            "total": total,
            "item_count": len(items),
            "trade_offs": trade_offs or [],
            "risks": risks or [],
        },
        plan_snapshot=items,
        constraint_snapshot=all_constraints,
    )


def log_cart_modification(
    session_id: str,
    user_id: str,
    actor: str,
    modification_type: str,
    item_ids: list[str] | None = None,
    old_total: float | None = None,
    new_total: float | None = None,
) -> AuditLogEntry:
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="cart_modified",
        data={
            "modification_type": modification_type,
            "item_ids": item_ids or [],
            "old_total": old_total,
            "new_total": new_total,
        },
    )


def log_review_confirmation(
    session_id: str,
    user_id: str,
    actor: str,
    final_items: list[dict],
    final_total: float,
    split_preview: dict | None = None,
    risks_acknowledged: bool = False,
    order_id: str | None = None,
    plan_id: str | None = None,
) -> AuditLogEntry:
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="review_confirmed",
        data={
            "final_total": final_total,
            "item_count": len(final_items),
            "split_preview": split_preview,
            "risks_acknowledged": risks_acknowledged,
            "order_id": order_id,
            "plan_id": plan_id,
        },
        plan_snapshot=final_items,
    )


def log_payment_completion(
    session_id: str,
    user_id: str,
    actor: str,
    order_id: str,
    payment_method: str,
    final_total: float,
    split_details: list[dict] | None = None,
) -> AuditLogEntry:
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="payment_completed",
        data={
            "order_id": order_id,
            "payment_method": payment_method,
            "final_total": final_total,
            "split_details": split_details or [],
        },
    )


def log_order_status_changed(
    session_id: str,
    user_id: str,
    actor: str,
    order_id: str,
    from_status: str,
    to_status: str,
    reason: str | None = None,
) -> AuditLogEntry:
    """写 order_status_changed 审计事件（第 9 个埋点，H1 状态机转移时调用）。

    data 携带 from_status / to_status / reason，是订单生命周期的时间轴证据。
    """
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="order_status_changed",
        data={
            "order_id": order_id,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
        },
    )


def log_conflict_detection(
    session_id: str,
    user_id: str,
    actor: str,
    conflicts: list[dict],
    suggestion: str | None = None,
) -> AuditLogEntry:
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="conflict_detected",
        data={
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "suggestion": suggestion,
        },
    )


def log_budget_violation(
    session_id: str,
    user_id: str,
    actor: str,
    budget_limit: float,
    actual_total: float,
    resolution: str,
) -> AuditLogEntry:
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="budget_violated",
        data={
            "budget_limit": budget_limit,
            "actual_total": actual_total,
            "resolution": resolution,
        },
    )


def log_fallback_trigger(
    session_id: str,
    user_id: str,
    actor: str,
    reason: str,
    suggested_action: str | None = None,
) -> AuditLogEntry:
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="fallback_triggered",
        data={
            "reason": reason,
            "suggested_action": suggested_action,
        },
    )


def log_payment_amount_mismatch(
    session_id: str,
    user_id: str,
    actor: str,
    order_id: str,
    captured_amount: float,
    expected_amount: float,
    paypal_token: str,
) -> AuditLogEntry:
    """写 D2 副门金额不匹配告警事件（独立于 budget_violated）。

    这是金额被篡改（captured_amount != order.total）的审计证据，
    性质与 D2 主门预算超支完全不同，必须用独立事件区分，
    否则审计时间线会把"金额被改"隐匿在预算超支里，形成审计盲区。
    """
    logger = get_audit_logger()
    return logger.log(
        session_id=session_id,
        user_id=user_id,
        actor=actor,
        event="payment_amount_mismatch",
        data={
            "order_id": order_id,
            "captured_amount": round(float(captured_amount), 2),
            "expected_amount": round(float(expected_amount), 2),
            "paypal_token": paypal_token,
        },
    )