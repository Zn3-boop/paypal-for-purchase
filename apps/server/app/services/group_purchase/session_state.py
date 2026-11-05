"""会话状态管理 —— E2 模块：购物车/方案/约束的状态持久化。

对应执行清单：
  E2 会话状态管理（session/cart/constraints 跨轮次持久化）
  G4 分摊预览持久化
  I 审计日志关联

设计要点：
  - 每个 session_id 对应一个完整的工作流状态
  - 支持状态快照（用于撤销/回滚）
  - 支持增量更新（只保存变更部分）
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.group_purchase.constraints import Constraint, ConstraintManager


@dataclass
class PlanItem:
    """方案中的单项商品。"""
    id: str
    name: str
    category: str
    price: float
    quantity: int = 1
    subtotal: float = 0.0
    source: str = "local_catalog"
    matched_constraints: list[str] = field(default_factory=list)
    rationale: str = ""
    
    def __post_init__(self):
        if self.subtotal == 0.0:
            self.subtotal = self.price * self.quantity
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "price": self.price,
            "quantity": self.quantity,
            "subtotal": self.subtotal,
            "source": self.source,
            "matched_constraints": self.matched_constraints,
            "rationale": self.rationale,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanItem":
        return cls(
            id=data["id"],
            name=data["name"],
            category=data.get("category", ""),
            price=data.get("price", 0),
            quantity=data.get("quantity", 1),
            subtotal=data.get("subtotal", 0),
            source=data.get("source", "local_catalog"),
            matched_constraints=data.get("matched_constraints", []),
            rationale=data.get("rationale", ""),
        )


@dataclass
class ShoppingPlan:
    """一个完整的采购方案。"""
    plan_id: str  # "A" or "B"
    strategy: str  # "低价优先" or "耐用优先"
    items: list[PlanItem]
    total: float
    currency: str = "USD"
    matched_constraints: list[str] = field(default_factory=list)
    unmet_constraints: list[str] = field(default_factory=list)
    trade_offs: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    source: str = "local_catalog"  # "channel3" | "local_catalog"
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "strategy": self.strategy,
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "currency": self.currency,
            "matched_constraints": self.matched_constraints,
            "unmet_constraints": self.unmet_constraints,
            "trade_offs": self.trade_offs,
            "risks": self.risks,
            "source": self.source,
            "generated_at": self.generated_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShoppingPlan":
        items = [PlanItem.from_dict(i) for i in data.get("items", [])]
        return cls(
            plan_id=data["plan_id"],
            strategy=data["strategy"],
            items=items,
            total=data["total"],
            currency=data.get("currency", "USD"),
            matched_constraints=data.get("matched_constraints", []),
            unmet_constraints=data.get("unmet_constraints", []),
            trade_offs=data.get("trade_offs", []),
            risks=data.get("risks", []),
            source=data.get("source", "local_catalog"),
            generated_at=datetime.fromisoformat(data.get("generated_at", datetime.now(timezone.utc).isoformat())),
        )


@dataclass
class SplitPreview:
    """分摊预览。"""
    member_splits: list[dict[str, Any]]  # [{"member_id": str, "amount": float}, ...]
    payment_method: str = "paypal_split"
    order_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "member_splits": self.member_splits,
            "payment_method": self.payment_method,
            "order_id": self.order_id,
            "created_at": self.created_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SplitPreview":
        return cls(
            member_splits=data.get("member_splits", []),
            payment_method=data.get("payment_method", "paypal_split"),
            order_id=data.get("order_id"),
            created_at=datetime.fromisoformat(data.get("created_at", datetime.now(timezone.utc).isoformat())),
        )


@dataclass
class GroupPurchaseSession:
    """团体采购会话的完整状态。"""
    session_id: str
    user_id: str
    state: str = "constraint_collection"  # constraint_collection | plan_generated | reviewing | confirmed | completed
    
    # A 模块：约束
    constraints: list[Constraint] = field(default_factory=list)
    constraint_manager: ConstraintManager | None = None
    
    # C 模块：双方案
    plan_a: ShoppingPlan | None = None
    plan_b: ShoppingPlan | None = None
    selected_plan: ShoppingPlan | None = None

    # GET /plans 首次生成的完整响应快照（快照语义：此后 GET 只回放，不再重算）。
    # 只有 reset 会让它失效 —— 否则约束一变，轮询就会静默改写用户在看的方案。
    plans_snapshot: dict[str, Any] | None = None
    
    # E 模块：购物车（改单后编辑中的商品列表）
    cart_items: list[PlanItem] = field(default_factory=list)
    
    # G 模块：分摊预览
    split_preview: SplitPreview | None = None
    
    # H 模块：订单
    order_id: str | None = None
    order_status: str | None = None
    
    # 元数据
    intent_query: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # 历史快照（用于撤销）
    history: list[dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "state": self.state,
            "constraints": [c.to_dict() for c in self.constraints],
            "plan_a": self.plan_a.to_dict() if self.plan_a else None,
            "plan_b": self.plan_b.to_dict() if self.plan_b else None,
            "selected_plan": self.selected_plan.to_dict() if self.selected_plan else None,
            "plans_snapshot": self.plans_snapshot,
            "cart_items": [item.to_dict() for item in self.cart_items],
            "split_preview": self.split_preview.to_dict() if self.split_preview else None,
            "order_id": self.order_id,
            "order_status": self.order_status,
            "intent_query": self.intent_query,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "history": self.history[-10:],  # 保留最近 10 个快照
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GroupPurchaseSession":
        constraints = [Constraint.from_dict(c) for c in data.get("constraints", [])]
        
        session = cls(
            session_id=data["session_id"],
            user_id=data["user_id"],
            state=data.get("state", "constraint_collection"),
            constraints=constraints,
            intent_query=data.get("intent_query"),
            created_at=datetime.fromisoformat(data.get("created_at", datetime.now(timezone.utc).isoformat())),
            updated_at=datetime.fromisoformat(data.get("updated_at", datetime.now(timezone.utc).isoformat())),
        )
        
        if data.get("plan_a"):
            session.plan_a = ShoppingPlan.from_dict(data["plan_a"])
        if data.get("plan_b"):
            session.plan_b = ShoppingPlan.from_dict(data["plan_b"])
        if data.get("selected_plan"):
            session.selected_plan = ShoppingPlan.from_dict(data["selected_plan"])
        if data.get("plans_snapshot"):
            session.plans_snapshot = data["plans_snapshot"]
        if data.get("cart_items"):
            session.cart_items = [PlanItem.from_dict(i) for i in data["cart_items"]]
        if data.get("split_preview"):
            session.split_preview = SplitPreview.from_dict(data["split_preview"])
        
        if data.get("order_id"):
            session.order_id = data["order_id"]
            session.order_status = data.get("order_status")
        
        if data.get("history"):
            session.history = data["history"]
        
        return session
    
    def save_snapshot(self, reason: str = "") -> None:
        """保存当前状态快照。"""
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "state": self.state,
            "cart_count": len(self.cart_items),
            "plan_selected": self.selected_plan is not None,
        }
        self.history.append(snapshot)
        self.updated_at = datetime.now(timezone.utc)
    
    def get_latest_cart(self) -> list[PlanItem]:
        """获取当前购物车（优先使用编辑后的 cart_items，否则从 selected_plan 复制）。"""
        if self.cart_items:
            return list(self.cart_items)
        if self.selected_plan:
            return [PlanItem(**item.to_dict()) for item in self.selected_plan.items]
        return []
    
    def reset_to_initial(self) -> None:
        """一键重置到初始状态。"""
        self.state = "constraint_collection"
        self.constraints = []
        self.plan_a = None
        self.plan_b = None
        self.selected_plan = None
        self.plans_snapshot = None
        self.cart_items = []
        self.split_preview = None
        self.order_id = None
        self.order_status = None
        self.save_snapshot(reason="manual_reset")


# ============================================================
# 会话管理器（内存存储，生产环境可替换为 Redis/DB）
# ============================================================

_sessions: dict[str, GroupPurchaseSession] = {}
_session_lock = threading.Lock()


def get_or_create_session(session_id: str, user_id: str) -> GroupPurchaseSession:
    """获取或创建会话。"""
    with _session_lock:
        if session_id not in _sessions:
            _sessions[session_id] = GroupPurchaseSession(
                session_id=session_id,
                user_id=user_id,
            )
        return _sessions[session_id]


def get_session(session_id: str) -> GroupPurchaseSession | None:
    """获取会话。"""
    with _session_lock:
        return _sessions.get(session_id)


def update_session(session: GroupPurchaseSession) -> None:
    """更新会话。"""
    with _session_lock:
        _sessions[session.session_id] = session


def delete_session(session_id: str) -> bool:
    """删除会话。"""
    with _session_lock:
        if session_id in _sessions:
            del _sessions[session_id]
            return True
        return False


def list_sessions(user_id: str | None = None) -> list[GroupPurchaseSession]:
    """列出会话。"""
    with _session_lock:
        if user_id:
            return [s for s in _sessions.values() if s.user_id == user_id]
        return list(_sessions.values())