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

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.paths import data_dir
from app.services.group_purchase.constraints import Constraint, ConstraintManager

logger = logging.getLogger(__name__)


# ============================================================
# 会话生命周期：滑动窗口 + 读路径惰性过期
# ============================================================
# 参考 Django Session 的读路径惰性判定（`expire_date__gt=now` 直接把过期行过滤掉）：
# **不是定时删除，而是"读的时候就不返回"**。本项目单进程、demo 规模（几百个 JSON），
# 定时清理只是额外复杂度；保证正确性只需要下面这一半。
#
# TTL 是**滑动窗口**：每次读/写都 touch 一次 last_active_at。
# 落盘策略参考 express-session 的 `touch()`：只在剩余时间不足一半时才写盘，
# 避免前端 3~5 秒一次的轮询把整个会话 JSON 反复重写。
DEFAULT_SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 小时


def get_session_ttl_seconds() -> float:
    """读取 TTL（`SESSION_TTL_SECONDS` 可覆盖；<=0 表示关闭过期，便于长演示）。"""
    try:
        return float(os.getenv("SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL_SECONDS)))
    except ValueError:
        return float(DEFAULT_SESSION_TTL_SECONDS)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    """`fromisoformat` 可能拿到 naive 时间（老文件 / 手写数据），统一按 UTC 解释。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def is_session_expired(session: "GroupPurchaseSession", now: datetime | None = None) -> bool:
    """会话是否已过 TTL（纯判定，不改状态）。

    老会话文件没有 last_active_at 时退化为 updated_at —— 这意味着**历史残留文件
    在升级后第一次被读到就会按真实空闲时长判过期**，正是想要的清理效果。
    """
    ttl = get_session_ttl_seconds()
    if ttl <= 0:
        return False
    last = _as_aware(session.last_active_at or session.updated_at)
    current = _as_aware(now or _utc_now())
    return (current - last).total_seconds() > ttl


def get_session_expires_in_seconds(session: "GroupPurchaseSession", now: datetime | None = None) -> int | None:
    """距离过期还剩多少秒（供前端倒计时）；TTL<=0（关闭过期）时返回 None。"""
    ttl = get_session_ttl_seconds()
    if ttl <= 0:
        return None
    last = _as_aware(session.last_active_at or session.updated_at)
    current = _as_aware(now or _utc_now())
    return max(0, int(ttl - (current - last).total_seconds()))


def touch_session(session: "GroupPurchaseSession") -> None:
    """标记会话「活跃」并落盘 —— **只允许写路径调用**。

    为什么读路径绝对不能 touch：前端每 3~5 秒轮询一次 `GET /session/{id}/status`、
    `GET /plans`、`GET /cart`。若读也续期，TTL 就永远走不到点 —— 用户开着标签页
    睡一晚，第二天回来旧约束/旧方案原样复活。那等于「做了一个 TTL，又用轮询把它废了」，
    比不做更糟：因为你会以为它生效了。

    因此 TTL 的分界线是**有没有产生新的业务事实**：
      - ✅ touch：提交需求、加/改/删购物车、选方案、复核、建单/支付
      - ❌ 不 touch：status、GET plans、GET cart、任何轮询与只读查询

    这同时也是 Django Session 的默认语义（`SESSION_SAVE_EVERY_REQUEST=False`）：
    只有会话被**修改**才刷新失效时间。
    """
    session.last_active_at = _utc_now()
    save_session_to_disk(session)



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
    # Channel3 证据字段（前端「推荐依据」直接消费；本地 catalog 时为空）：
    image_url: str = ""
    tags: list[str] = field(default_factory=list)
    match_score: float = 0.0
    match_tags: list[str] = field(default_factory=list)
    
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
            "image_url": self.image_url,
            "tags": self.tags,
            "match_score": self.match_score,
            "match_tags": self.match_tags,
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
            image_url=data.get("image_url", ""),
            tags=list(data.get("tags") or []),
            match_score=float(data.get("match_score") or 0),
            match_tags=list(data.get("match_tags") or []),
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
    # 统一入口的模式标记：single（单人购买）/ group（团购拼单）。
    # 两种模式共用同一条「约束 → 检索 → 双方案 → 支付」流水线，只有预算/分摊展示不同。
    intent_mode: str = "group"
    # 由「统一搜索框」提交时自动抽取的约束 id：
    #   - 每次新提问会整体替换这一批（旧任务的口味/预算不该污染新任务）；
    #   - 用户在约束看板里**手动添加**的约束不在这个列表里，因此不会被清掉。
    auto_constraint_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # 最后一次读/写时间（滑动窗口）。TTL 空闲判定只用它，不参与业务逻辑。
    # 老文件没有这个字段时按 updated_at 兜底（见 from_dict / is_session_expired）。
    last_active_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
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
            "intent_mode": self.intent_mode,
            "auto_constraint_ids": self.auto_constraint_ids,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_active_at": self.last_active_at.isoformat(),
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
            intent_mode=data.get("intent_mode", "group"),
            auto_constraint_ids=list(data.get("auto_constraint_ids") or []),
            created_at=datetime.fromisoformat(data.get("created_at", datetime.now(timezone.utc).isoformat())),
            updated_at=datetime.fromisoformat(data.get("updated_at", datetime.now(timezone.utc).isoformat())),
            # 老文件没有 last_active_at：退回 updated_at（真实空闲时长），
            # 让残留会话在升级后第一次读到就按实际过期被清理，而不是被"续命"。
            last_active_at=datetime.fromisoformat(
                data.get("last_active_at")
                or data.get("updated_at")
                or data.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            ),
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

# ============================================================
# 会话磁盘持久化：后端重启后，约束 / 方案 / 购物车 / 订单号都还在
# ============================================================
# 为什么是「每会话一个 JSON」而不是 JSONL：会话是**可变状态**（覆盖写），
# 不是事件流；JSONL 适合审计/订单那种 append-only 事实。这里用原子写
# （tmp + os.replace）保证进程被杀时不会留下半个 JSON。
_SESSION_SCHEMA_VERSION = 1


def get_sessions_dir() -> Path:
    """会话目录（`SESSION_STORE_DIR` 可覆盖；默认锚定 apps/server/data，不随 cwd 漂移）。"""
    return data_dir("SESSION_STORE_DIR", "sessions")


def get_session_file(session_id: str) -> Path:
    """session_id → 安全文件名（只保留字母数字与 ._-，防止路径穿越）。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "")[:120] or "unknown"
    return get_sessions_dir() / f"{safe}.json"


def save_session_to_disk(session: GroupPurchaseSession) -> bool:
    """把会话写盘。失败只记日志，绝不阻断业务流程（支付链路优先）。"""
    try:
        payload = session.to_dict()
        payload["schema_version"] = _SESSION_SCHEMA_VERSION
        path = get_session_file(session.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("session persist failed (%s): %s", session.session_id, exc)
        return False


def _load_session_file(session_id: str) -> GroupPurchaseSession | None:
    path = get_session_file(session_id)
    if not path.exists():
        return None
    try:
        return GroupPurchaseSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("session restore failed (%s): %s", session_id, exc)
        return None


def load_sessions_from_disk() -> int:
    """启动时回放 data/sessions/*.json；内存里已有的会话不会被覆盖。

    过期会话直接跳过并删除文件 —— 惰性清理的"启动时那一半"：
    进程重启是比较少见的事件，顺手删掉已过 TTL 的残留，避免目录无限增长。
    """
    directory = get_sessions_dir()
    if not directory.exists():
        return 0
    restored = 0
    expired = 0
    for path in sorted(directory.glob("*.json")):
        try:
            session = GroupPurchaseSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("session restore skipped (%s): %s", path.name, exc)
            continue
        if is_session_expired(session):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
            expired += 1
            continue
        with _session_lock:
            if session.session_id not in _sessions:
                _sessions[session.session_id] = session
                restored += 1
    if expired:
        logger.info("session expired at startup: %d file(s) removed", expired)
    return restored


def get_or_create_session(session_id: str, user_id: str) -> GroupPurchaseSession:
    """获取或创建会话。

    过期会话视同不存在：直接丢弃（内存 + 磁盘）并新建一个空会话。
    这是「跨天回来看到上次的约束/方案/购物车」这个问题的修复点 ——
    不需要用户点任何按钮，也不需要换 user_id。
    """
    existing = get_session(session_id)
    if existing is not None:
        return existing
    with _session_lock:
        # 并发下另一个线程可能已经建好了；以内存里最先落位的为准。
        session = _sessions.get(session_id)
        if session is None:
            session = GroupPurchaseSession(session_id=session_id, user_id=user_id)
            _sessions[session_id] = session
        save_session_to_disk(session)
        return session


def get_session(session_id: str) -> GroupPurchaseSession | None:
    """获取会话：内存优先；内存没有（例如刚重启）就从磁盘 JSON 恢复。

    **纯读，不 touch、不写盘。** 过期判定在**读路径**完成（Django Session 的做法）：
    过期就是 None，上层 API 自然返回 404 / exists=false，前端已有的自举逻辑
    会自动用同一个 session_id 建新会话。

    为什么读不续期：轮询也算"读"。详情见 touch_session 的文档。
    """
    with _session_lock:
        cached = _sessions.get(session_id)
    if cached is not None:
        if is_session_expired(cached):
            delete_session(session_id)
            logger.info("session expired on read: %s", session_id)
            return None
        return cached
    restored = _load_session_file(session_id)
    if restored is None:
        return None
    if is_session_expired(restored):
        delete_session(session_id)
        logger.info("session expired on restore: %s", session_id)
        return None
    with _session_lock:
        return _sessions.setdefault(session_id, restored)


def update_session(session: GroupPurchaseSession) -> None:
    """更新会话（写路径）：刷新 last_active_at + 内存 + 磁盘（原子写）。

    这是 TTL 唯一会续期的地方 —— 所有业务写操作最终都收敛到这里。
    """
    session.last_active_at = _utc_now()
    session.updated_at = session.last_active_at
    with _session_lock:
        _sessions[session.session_id] = session
    save_session_to_disk(session)


def delete_session(session_id: str) -> bool:
    """删除会话（内存 + 磁盘文件）。"""
    with _session_lock:
        existed = _sessions.pop(session_id, None) is not None
    try:
        get_session_file(session_id).unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        logger.warning("session file delete failed (%s): %s", session_id, exc)
    return existed


def list_sessions(user_id: str | None = None) -> list[GroupPurchaseSession]:
    """列出会话（过期的不列出 —— 与 get_session 的读路径判定保持一致）。"""
    with _session_lock:
        sessions = list(_sessions.values())
    sessions = [s for s in sessions if not is_session_expired(s)]
    if user_id:
        return [s for s in sessions if s.user_id == user_id]
    return sessions