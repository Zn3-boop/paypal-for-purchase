"""约束系统 —— 团体采购的多成员约束管理与冲突检测。

对应执行清单 A 模块：
  A1 定义 Constraint 数据结构
  A2 全局约束状态对象（会话级）
  A3/A4/A5 后续由前端调用此 API

持久化：会话级内存（演示），可扩展到 SQLite/JSON 文件。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

ConstraintType = Literal["budget", "functional", "taboo", "preference"]
ConstraintStatus = Literal["active", "modified", "removed"]

# 预算冲突判定阈值：两位成员的预算报价相差超过该倍数即视为冲突
BUDGET_CONFLICT_RATIO = 1.4


@dataclass
class Constraint:
    """一条结构化约束。
    
    A1: 约束数据结构定义
    """
    id: str
    type: ConstraintType
    content: str  # 自然语言描述，如"总预算≤1200"
    hard: bool    # 硬约束（违反则无解）vs 软约束（可权衡）
    source: str   # 成员标识，如"成员 A"、"member_1"
    status: ConstraintStatus = "active"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    # 结构化字段（可选，用于精确计算）
    numeric_value: float | None = None  # 如预算金额
    category: str | None = None         # 如"环保材料"
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "hard": self.hard,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "numeric_value": self.numeric_value,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Constraint":
        """从会话 JSON 恢复（与 to_dict 对称；未知 type/status 回落到安全默认）。"""
        ctype = data.get("type")
        if ctype not in ("budget", "functional", "taboo", "preference"):
            ctype = "functional"
        status = data.get("status")
        if status not in ("active", "modified", "removed"):
            status = "active"
        numeric = data.get("numeric_value")
        if numeric is not None:
            try:
                numeric = float(numeric)
            except (TypeError, ValueError):
                numeric = None
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            id=str(data.get("id") or ""),
            type=ctype,
            content=str(data.get("content") or ""),
            hard=bool(data.get("hard", False)),
            source=str(data.get("source") or ""),
            status=status,
            created_at=str(data.get("created_at") or now),
            updated_at=str(data.get("updated_at") or now),
            numeric_value=numeric,
            category=data.get("category"),
        )


@dataclass
class ConstraintConflict:
    """两条互斥约束的冲突。
    
    A4: 冲突高亮所需信息
    """
    constraint_a: Constraint
    constraint_b: Constraint
    reason: str  # 解释为何互斥
    
    def to_dict(self) -> dict:
        return {
            "constraint_a": self.constraint_a.to_dict(),
            "constraint_b": self.constraint_b.to_dict(),
            "reason": self.reason,
        }


class ConstraintManager:
    """会话级约束管理器。key = session_id。
    
    A2: 全局约束状态对象（会话级读写/增删改 API）
    """
    
    def __init__(self) -> None:
        self._store: dict[str, list[Constraint]] = {}  # session_id -> [Constraint]
        self._lock = threading.RLock()  # RLock 支持同一线程递归加锁
        self._conflict_cache: dict[str, list[ConstraintConflict]] = {}
    
    def add_constraint(self, session_id: str, constraint: Constraint) -> Constraint:
        """添加一条新约束。"""
        with self._lock:
            self._store.setdefault(session_id, []).append(constraint)
            self._conflict_cache.pop(session_id, None)  # 清空冲突缓存
            return constraint
    
    def add_constraint_from_text(
        self, 
        session_id: str, 
        text: str, 
        source: str,
        constraint_type: ConstraintType = "budget",
        hard: bool = False,
        numeric_value: float | None = None
    ) -> Constraint:
        """从自然语言添加约束（解析逻辑后续由 B2 Prompt 填充）。
        
        目前用简单规则提取数字，后续替换为 LLM 提取。
        """
        import re
        constraint_id = f"c_{session_id}_{len(self._store.get(session_id, [])) + 1}"
        
        # 简单数字提取（后续由 B2 替代）
        if numeric_value is None:
            match = re.search(r"(\d[\d,.]*)", text)
            if match:
                try:
                    numeric_value = float(match.group(1).replace(",", ""))
                except ValueError:
                    pass
        
        constraint = Constraint(
            id=constraint_id,
            type=constraint_type,
            content=text.strip(),
            hard=hard,
            source=source,
            numeric_value=numeric_value,
        )
        return self.add_constraint(session_id, constraint)
    
    def get_constraints(self, session_id: str, active_only: bool = True) -> list[Constraint]:
        """获取会话的所有约束。"""
        with self._lock:
            constraints = self._store.get(session_id, [])
            if active_only:
                return [c for c in constraints if c.status == "active"]
            return list(constraints)
    
    def update_constraint(
        self, 
        session_id: str, 
        constraint_id: str, 
        **updates
    ) -> Constraint | None:
        """更新一条约束。"""
        with self._lock:
            for i, c in enumerate(self._store.get(session_id, [])):
                if c.id == constraint_id:
                    if "content" in updates:
                        c.content = updates["content"]
                    if "hard" in updates:
                        c.hard = updates["hard"]
                    if "status" in updates:
                        c.status = updates["status"]
                    if "numeric_value" in updates:
                        c.numeric_value = updates["numeric_value"]
                    if "type" in updates:
                        c.type = updates["type"]
                    c.updated_at = datetime.now(timezone.utc).isoformat()
                    self._conflict_cache.pop(session_id, None)
                    return c
            return None
    
    def remove_constraint(self, session_id: str, constraint_id: str) -> bool:
        """移除一条约束（软删除，标记为 removed）。"""
        with self._lock:
            for i, c in enumerate(self._store.get(session_id, [])):
                if c.id == constraint_id:
                    c.status = "removed"
                    c.updated_at = datetime.now(timezone.utc).isoformat()
                    self._conflict_cache.pop(session_id, None)
                    return True
            return False
    
    def detect_conflicts(self, session_id: str) -> list[ConstraintConflict]:
        """检测约束冲突。
        
        A4: 冲突高亮核心逻辑
        目前实现：预算冲突（同类型但数值差距过大）
        后续扩展：功能冲突、偏好冲突
        """
        with self._lock:
            # 使用缓存
            if session_id in self._conflict_cache:
                return self._conflict_cache[session_id]
            
            constraints = self.get_constraints(session_id, active_only=True)
            conflicts: list[ConstraintConflict] = []
            
            # 预算冲突检测
            budget_constraints = [c for c in constraints if c.type == "budget" and c.hard]
            for i, a in enumerate(budget_constraints):
                for b in budget_constraints[i+1:]:
                    if a.numeric_value and b.numeric_value:
                        ratio = max(a.numeric_value, b.numeric_value) / min(a.numeric_value, b.numeric_value)
                        if ratio >= BUDGET_CONFLICT_RATIO:  # 差距超过阈值视为冲突
                            conflicts.append(ConstraintConflict(
                                constraint_a=a,
                                constraint_b=b,
                                reason=f"预算差距过大：{a.content} vs {b.content}（比例 {ratio:.1f}x）",
                            ))
            
            # TODO: 后续扩展更多冲突类型
            # - 功能冲突：要环保但又要便宜（环保材料通常更贵）
            # - 偏好冲突：成员 A 喜欢红色，成员 B 讨厌红色
            
            self._conflict_cache[session_id] = conflicts
            return conflicts
    
    def get_constraints_by_source(self, session_id: str) -> dict[str, list[Constraint]]:
        """按来源成员分组约束。"""
        constraints = self.get_constraints(session_id)
        grouped: dict[str, list[Constraint]] = {}
        for c in constraints:
            grouped.setdefault(c.source, []).append(c)
        return grouped
    
    def clear_session(self, session_id: str) -> None:
        """清空会话的所有约束。"""
        with self._lock:
            self._store.pop(session_id, None)
            self._conflict_cache.pop(session_id, None)


# 全局 registry（演示用）
_registry: dict[str, ConstraintManager] = {}
_registry_lock = threading.Lock()


def get_constraint_manager(session_id: str | None = None) -> ConstraintManager:
    """按 session_id 获取约束管理器实例。"""
    key = session_id or "default"
    with _registry_lock:
        manager = _registry.get(key)
        if manager is None:
            manager = ConstraintManager()
            _registry[key] = manager
        return manager