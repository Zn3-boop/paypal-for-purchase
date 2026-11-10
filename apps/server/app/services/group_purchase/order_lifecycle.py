"""订单生命周期状态机（H1 模块）+ 订单档案库（H2/H3 数据底座）。

    draft ──▶ confirmed ──▶ paying ──▶ paid
                  │            │           ▲
                  │            ├──▶ expired ┘   (读路径惰性判定；支付事实可推翻)
                  └──▶ cancelled ◀──┘

转移守卫（Guard，违反则结构上无法转移）：
  - draft → confirmed  ：审计链必须存在匹配该订单的 review_confirmed 事件
                         （HITL 从「前端按钮顺序」升级为「后端强制」——
                          没有 review 记录，进不了 paying）
  - confirmed → paying ：check_budget(session_total) PASS
                         （D2 主门以状态机转移守卫的形态落地）
  - paying → paid      ：capture 成功 且 captured_amount == 订单金额
                         （D2 副门：金额一致性纵深防御）
  - paying → cancelled ：用户取消 / PayPal 回调 cancelled / 金额不符告警
  - paying → expired   ：读路径发现已过 expires_at（本地推断，见 expire_if_stale）
  - expired → paid     ：外部事实优先 —— PayPal capture 已 COMPLETED 时推翻本地过期判定

关键设计：
  - 幂等：目标状态 == 当前状态时返回成功但不重复记审计，PayPal 回调重试安全。
  - 每次成功转移写第 9 个审计事件 order_status_changed（from/to/order_id/reason）。
  - 业务订单号（ord_xxx）贯穿全生命周期；paypal_token 仅是网关侧令牌。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from app.services.group_purchase.audit_log import (
    get_audit_logger,
    log_order_status_changed,  # 第 9 个审计埋点
)
from app.services.group_purchase.session_state import get_session, update_session

logger = logging.getLogger(__name__)


class OrderStatus(str, Enum):
    """订单状态枚举（H1 状态机节点）。"""

    DRAFT = "draft"            # 已创建订单记录，等待人工复核
    CONFIRMED = "confirmed"    # 人工复核通过（review_confirmed 事件已落审计）
    PAYING = "paying"          # 已向 PayPal 发起支付，等待回跳
    PAID = "paid"              # 支付完成（终态）
    CANCELLED = "cancelled"    # 已取消 / 金额不符告警（终态）
    EXPIRED = "expired"        # 支付窗口已过（本地推断；可被支付事实推翻）


# 合法转移表：key = 当前状态，value = 允许到达的状态集合
_ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.DRAFT: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.PAYING, OrderStatus.CANCELLED},
    OrderStatus.PAYING: {OrderStatus.PAID, OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    # 外部事实优先：迟到的成功回跳可以推翻本地「过期」推断（金额守卫仍然生效）
    OrderStatus.EXPIRED: {OrderStatus.PAID},
    OrderStatus.PAID: set(),
    OrderStatus.CANCELLED: set(),
}


@dataclass
class OrderRecord:
    """业务订单记录：order_id 为本地业务订单号，贯穿 draft → paid 全程。

    items 为创建时的购物车快照（list[dict]），与审计 plan_snapshot 同构。
    """

    order_id: str
    session_id: str
    user_id: str
    total: float = 0.0
    currency: str = "USD"
    plan_id: str | None = None
    paypal_token: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    status: OrderStatus = OrderStatus.DRAFT
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cancelled_reason: str | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    # paying 的支付窗口截止时间（ISO8601）。None = 未进入 paying / 老数据（不判超时）。
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "total": self.total,
            "currency": self.currency,
            "plan_id": self.plan_id,
            "paypal_token": self.paypal_token,
            "items_count": len(self.items),
            "items": self.items,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cancelled_reason": self.cancelled_reason,
            "transitions": self.transitions,
            "expires_at": self.expires_at,
        }

    def brief(self) -> dict[str, Any]:
        """轻量视图（列表页 / 轮询用，不含商品明细）。"""
        return {
            "order_id": self.order_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "total": self.total,
            "currency": self.currency,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cancelled_reason": self.cancelled_reason,
            "expires_at": self.expires_at,
        }


# ============================================================
# 进程内订单注册表（内存演示级，与 session_state 同模式）
# ============================================================

_orders: dict[str, OrderRecord] = {}
_orders_lock = threading.Lock()


def _new_order_id() -> str:
    return f"ord_{uuid.uuid4().hex[:8]}"


# ============================================================
# 订单档案落盘（JSONL 追加写）：重启后「📦 订单档案」不丢
#   - 每条状态变化追加一行完整记录，同一 order_id 以最后一行为准
#   - 用 ORDER_STORE_DIR 覆盖存储目录（测试/部署可用）
# ============================================================

_ORDERS_FILE_NAME = "orders.jsonl"
_disk_lock = threading.Lock()


def get_orders_store_dir() -> Path:
    return Path(os.getenv("ORDER_STORE_DIR", "./data/orders"))


def get_orders_file_path() -> Path:
    store_dir = get_orders_store_dir()
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir / _ORDERS_FILE_NAME


def _append_to_disk(order: OrderRecord) -> None:
    try:
        with _disk_lock:
            with open(get_orders_file_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(order.to_dict(), ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - 落盘失败不应阻断支付主链路
        logger.warning("order persist failed for %s: %s", order.order_id, exc)


def order_from_dict(data: dict[str, Any]) -> OrderRecord:
    """把 JSONL 中的一行还原为 OrderRecord（缺字段时使用安全默认值）。"""
    raw_status = str(data.get("status") or OrderStatus.DRAFT.value)
    try:
        status = OrderStatus(raw_status)
    except ValueError:
        status = OrderStatus.DRAFT
    now = datetime.now(timezone.utc).isoformat()
    return OrderRecord(
        order_id=str(data["order_id"]),
        session_id=str(data["session_id"]),
        user_id=str(data.get("user_id") or "demo-user"),
        total=float(data.get("total") or 0.0),
        currency=str(data.get("currency") or "USD"),
        plan_id=data.get("plan_id"),
        paypal_token=data.get("paypal_token"),
        items=list(data.get("items") or []),
        status=status,
        created_at=str(data.get("created_at") or now),
        updated_at=str(data.get("updated_at") or now),
        cancelled_reason=data.get("cancelled_reason"),
        transitions=list(data.get("transitions") or []),
        expires_at=data.get("expires_at"),
    )


def load_orders_from_disk() -> int:
    """从 JSONL 恢复订单档案（进程重启后调用）。返回恢复的订单条数。

    只做增量补齐（setdefault）：已在内存中的订单以运行时状态为准，
    避免磁盘上的旧行覆盖正在支付的订单。
    """
    path = get_orders_file_path()
    if not path.exists():
        return 0

    latest: dict[str, OrderRecord] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = order_from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                latest[record.order_id] = record  # 追加写：后出现的行为准
    except OSError as exc:  # pragma: no cover
        logger.warning("order store read failed: %s", exc)
        return 0

    if not latest:
        return 0

    with _orders_lock:
        for order_id, record in latest.items():
            _orders.setdefault(order_id, record)

    logger.info("order store restored %d records from %s", len(latest), path)
    return len(latest)


def get_order_record(order_id: str) -> OrderRecord | None:
    with _orders_lock:
        order = _orders.get(order_id)
    # 读路径惰性判定：必须在释放 _orders_lock 之后（转移内部会再次加锁写盘）
    if order is not None:
        expire_if_stale(order)
    return order


def list_order_records(session_id: str | None = None) -> list[OrderRecord]:
    with _orders_lock:
        records = list(_orders.values())
    if session_id:
        records = [r for r in records if r.session_id == session_id]
    # 读路径惰性判定（锁已释放）：超时的 paying 在列表里直接呈现为 expired
    for record in records:
        expire_if_stale(record)
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records


def _save(order: OrderRecord) -> None:
    """持久化订单记录：内存注册表 + JSONL 追加写（重启后可恢复）。"""
    order.updated_at = datetime.now(timezone.utc).isoformat()
    with _orders_lock:
        _orders[order.order_id] = order
    _append_to_disk(order)


# ============================================================
# 支付窗口（惰性过期）
#   进入 paying 写 expires_at；读路径发现超时 → expired（本地推断）
#   不引入定时器：没有常驻循环，也就没有新的故障点
# ============================================================

ORDER_PAYING_TTL_SECONDS = 1800  # 30 分钟；录制 3 分钟不会自然过期


def get_paying_ttl_seconds() -> int:
    """支付窗口时长（秒）。

    环境变量 ORDER_PAYING_TTL_SECONDS 可覆盖（测试用短 TTL，演示/生产用 30 分钟）。
    """
    raw = os.getenv("ORDER_PAYING_TTL_SECONDS", str(ORDER_PAYING_TTL_SECONDS))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return ORDER_PAYING_TTL_SECONDS


def payment_window_closed(order: OrderRecord, now: datetime | None = None) -> bool:
    """paying 且已越过 expires_at → True。

    expires_at 缺失（老数据 / 尚未进入 paying）一律视为「没有窗口」，不误杀。
    """
    if order.status != OrderStatus.PAYING or not order.expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(order.expires_at)
    except ValueError:
        return False
    if deadline.tzinfo is None:  # 兼容历史上可能写入的 naive 时间
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) > deadline


def expire_if_stale(
    order: OrderRecord,
    *,
    actor: str = "lazy_expiry",
    now: datetime | None = None,
) -> tuple[bool, str]:
    """读路径惰性过期：paying + 超时 → expired，并写 order_status_changed。

    返回 (是否发生转移, 描述)。幂等：非 paying / 未超时 / 已过期都返回 false。
    「过期」只是本地推断，PayPal 回跳 COMPLETED 时可以 expired → paid 推翻。
    """
    if not payment_window_closed(order, now):
        return False, ""
    return transition_order(
        order,
        OrderStatus.EXPIRED,
        actor=actor,
        reason=(
            f"支付窗口已过（expires_at={order.expires_at}"
            f"，TTL={get_paying_ttl_seconds()}s）：本地推断，支付事实可推翻"
        ),
    )


# ============================================================
# 转移守卫（Guard）
# ============================================================


def guard_review_confirmed(order: OrderRecord) -> tuple[bool, str]:
    """draft → confirmed 守卫：审计链必须有匹配该订单的 review_confirmed 事件。

    即使有人绕过前端直接调用支付 API，没有 review 记录也进不了 paying。
    """
    try:
        logs = get_audit_logger().get_session_timeline(order.session_id)
        for entry in logs:
            if entry.event == "review_confirmed" and entry.data.get("order_id") == order.order_id:
                return True, "review_confirmed 审计事件存在"
    except Exception as exc:  # 审计查询失败按 fail-closed 处理
        logger.warning("guard_review_confirmed query failed: %s", exc)
    return (
        False,
        f"拒绝转移:未找到匹配该订单({order.order_id})的 review_confirmed 审计事件,"
        "请先在复核面板完成人工确认",
    )


def guard_budget_passed(budget_check: dict[str, Any] | None) -> tuple[bool, str]:
    """confirmed → paying 守卫：带 check_budget 结果时校验 PASS。

    budget_check 为 None 表示会话未设置硬预算，跳过（无上限则无从超支）。
    """
    if budget_check is None:
        return True, "会话未设置硬预算，跳过预算守卫"
    if not budget_check.get("ok", False):
        return (
            False,
            f"预算守卫未通过:{budget_check.get('resolution', '超支')}"
            "（未创建 PayPal 订单，请调整购物车或提高预算）",
        )
    return True, "预算检查通过"


def _amount_to_cents(value: Any) -> int:
    """将金额转为分整数，避免 float 比较和容差放行问题。"""
    amount = Decimal(str(value))
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ============================================================
# 启动对账：扫 payment_completed 有但订单仍 paying → 补偿转移到 paid
# ============================================================

def reconcile_sessions_on_startup() -> int:
    """启动对账：先恢复订单档案，再补偿「审计已有 payment_completed 但订单仍 paying」。

    返回补偿次数，便于日志观察。
    """
    from app.services.group_purchase.audit_log import get_audit_logger, log_order_status_changed

    restored = load_orders_from_disk()
    if restored:
        logger.info("startup: restored %d order records from disk", restored)

    comp_count = 0
    # 注意：这里必须读「原始」注册表，不能走 list_order_records()——
    # 后者带惰性过期判定，会把「审计已有 payment_completed」的订单先标成 expired。
    # 对账优先级：外部/审计事实 > 本地时间推断。
    with _orders_lock:
        pending = list(_orders.values())  # 无 session_id 过滤，遍历全库
    for order in pending:
        if order.status != OrderStatus.PAYING:
            continue
        # 查该 session 审计链是否存在 payment_completed 且 order_id 匹配
        try:
            logs = get_audit_logger().get_session_timeline(order.session_id)
            found = any(
                e.event == "payment_completed" and e.data.get("order_id") == order.order_id
                for e in logs
            )
        except Exception:  # pragma: no cover
            found = False
        if not found:
            continue
        # 补偿：直接转移 paid（不再走 guard_amount_matched，因为审计事实已落盘）
        order.status = OrderStatus.PAID
        order.updated_at = datetime.now(timezone.utc).isoformat()
        order.transitions.append({
            "from": OrderStatus.PAYING.value,
            "to": OrderStatus.PAID.value,
            "at": order.updated_at,
            "actor": "startup_reconcile",
            "reason": "对账补偿：审计已有 payment_completed，订单仍 paying，强制迁至 paid",
        })
        _save(order)
        # 写一次补录审计（幂等：同一订单同一 session 只补一次）
        try:
            log_order_status_changed(
                session_id=order.session_id,
                user_id=order.user_id,
                actor="startup_reconcile",
                order_id=order.order_id,
                from_status=OrderStatus.PAYING.value,
                to_status=OrderStatus.PAID.value,
                reason="对账补偿：审计已有 payment_completed，订单仍 paying，强制迁至 paid",
            )
        except Exception:  # pragma: no cover
            logger.warning("reconcile write order_status_changed failed for %s", order.order_id)
        comp_count += 1
    return comp_count



def guard_amount_matched(
    captured_amount: float,
    expected_total: float,
) -> tuple[bool, str]:
    """paying → paid 守卫：PayPal 实扣金额必须与订单金额严格一致（D2 副门）。

    PayPal 返回的金额通常是字符串；比较前统一转换为“分”整数。
    因此 26.00 与 26.01 会被严格区分，不依赖 float 容差。
    """
    captured_cents = _amount_to_cents(captured_amount)
    expected_cents = _amount_to_cents(expected_total)
    if captured_cents != expected_cents:
        return (
            False,
            f"金额不一致告警:PayPal 实扣 {captured_cents / 100:.2f},订单金额 {expected_cents / 100:.2f},"
            "已阻止进入 paid 并回退 cancelled",
        )
    return True, f"实扣金额与订单金额一致（{captured_cents / 100:.2f}）"


# ============================================================
# 状态转移核心
# ============================================================


def transition_order(
    order: OrderRecord,
    target: OrderStatus,
    *,
    actor: str,
    reason: str | None = None,
    reason_code: str | None = None,
    guard: Callable[[], tuple[bool, str]] | None = None,
) -> tuple[bool, str]:
    """执行一次状态转移。

    返回 (ok, message)：
      - 幂等：target == 当前状态 → (True, "幂等跳过")，不重复记审计
      - 非法：不在迁移表     → (False, 错误信息)，状态不变
      - 守卫失败             → (False, 错误信息)，状态不变
      - 成功               → (True, 转移描述)，记 order_status_changed 审计

    reason_code：机器可读的原因码（如 expired→paid 的
    ``remote_completed_after_local_expiry``），随审计事件落盘，Q&A 可 grep 复算。
    """
    if order.status == target:
        return True, f"订单已是 {target.value}，幂等跳过（不重复记账）"

    if target not in _ALLOWED_TRANSITIONS.get(order.status, set()):
        return False, f"非法转移:{order.status.value} → {target.value}"

    if guard is not None:
        ok, msg = guard()
        if not ok:
            return False, msg

    prev = order.status
    order.status = target
    order.cancelled_reason = reason if target == OrderStatus.CANCELLED else None
    order.transitions.append(
        {
            "from": prev.value,
            "to": target.value,
            "at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "reason": reason,
            "reason_code": reason_code,
        }
    )
    _save(order)

    # 同步会话侧的 order_id / order_status（旧字段兼容）
    try:
        session = get_session(order.session_id)
        if session is not None:
            session.order_id = order.order_id
            session.order_status = order.status.value
            update_session(session)
    except Exception as exc:  # pragma: no cover - 不应阻断转移
        logger.warning("session order_status sync failed: %s", exc)

    # 第 9 个审计事件:order_status_changed
    try:
        log_order_status_changed(
            session_id=order.session_id,
            user_id=order.user_id,
            actor=actor,
            order_id=order.order_id,
            from_status=prev.value,
            to_status=target.value,
            reason=reason,
            reason_code=reason_code,
        )
    except Exception as exc:  # pragma: no cover - 审计失败不阻断主链路
        logger.warning("audit order_status_changed failed: %s", exc)

    return True, f"订单状态:{prev.value} → {target.value}"


# ============================================================
# 领域转移封装
# ============================================================


def create_order_record(
    session_id: str,
    user_id: str,
    *,
    total: float,
    items: list[dict[str, Any]],
    plan_id: str | None = None,
    currency: str = "USD",
) -> OrderRecord:
    """创建订单记录（初始状态 draft）。"""
    order = OrderRecord(
        order_id=_new_order_id(),
        session_id=session_id,
        user_id=user_id,
        total=round(float(total), 2),
        currency=currency,
        plan_id=plan_id,
        items=items,
        status=OrderStatus.DRAFT,
    )
    _save(order)
    return order


def confirm_order(order: OrderRecord, actor: str = "user") -> tuple[bool, str]:
    """draft → confirmed。守卫：存在匹配该订单的 review_confirmed 事件。"""
    return transition_order(
        order,
        OrderStatus.CONFIRMED,
        actor=actor,
        reason="人工复核通过",
        guard=lambda: guard_review_confirmed(order),
    )


def begin_payment(
    order: OrderRecord,
    actor: str = "system",
    *,
    budget_check: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """confirmed → paying。守卫：check_budget PASS（D2 主门）。

    进入 paying 的同时开支付窗口（expires_at）。幂等：已在 paying 且已开窗时不延长，
    避免「反复点支付 / 回调重试」把过期时间无限续下去。
    """
    ok, msg = transition_order(
        order,
        OrderStatus.PAYING,
        actor=actor,
        reason="发起 PayPal 支付",
        guard=lambda: guard_budget_passed(budget_check),
    )
    if ok and order.status == OrderStatus.PAYING and not order.expires_at:
        order.expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=get_paying_ttl_seconds())
        ).isoformat()
        _save(order)
    return ok, msg


def complete_payment(
    order: OrderRecord,
    captured_amount: float,
    actor: str = "paypal_callback",
    *,
    expected_total: float | None = None,
    reason: str | None = None,
) -> tuple[bool, str]:
    """paying / expired → paid。守卫：captured_amount == 订单金额（D2 副门）。

    expired → paid 是「外部事实优先」的唯一入口：本地时间推断被 PayPal 的
    capture COMPLETED 推翻时，审计 reason 必须写明原因，便于回放复核。
    """
    expected = expected_total if expected_total is not None else order.total
    default_reason = f"PayPal capture 成功，金额 {float(captured_amount):.2f}"
    if reason is None and order.status == OrderStatus.EXPIRED:
        default_reason = (
            f"本地判定已过期，但 PayPal capture 已完成（金额 {float(captured_amount):.2f}），"
            "以支付事实为准"
        )
    reason_code = (
        "remote_completed_after_local_expiry"
        if order.status == OrderStatus.EXPIRED
        else None
    )
    return transition_order(
        order,
        OrderStatus.PAID,
        actor=actor,
        reason=reason or default_reason,
        reason_code=reason_code,
        guard=lambda: guard_amount_matched(captured_amount, expected),
    )


def cancel_order(
    order: OrderRecord,
    actor: str = "user",
    *,
    reason: str = "用户取消",
) -> tuple[bool, str]:
    """draft / confirmed / paying → cancelled（paid 之后不可取消；expired 也不可取消）。"""
    if order.status == OrderStatus.EXPIRED:
        return False, "订单已超时过期（expired），不可取消；若收到支付成功回跳将以支付事实为准"
    return transition_order(
        order,
        OrderStatus.CANCELLED,
        actor=actor,
        reason=reason,
    )


def attach_paypal_token(order: OrderRecord, token: str) -> None:
    """绑定网关侧 PayPal 令牌（进入 paying 后写入）。"""
    order.paypal_token = token
    _save(order)


def refresh_order_snapshot(
    order: OrderRecord,
    *,
    total: float,
    items: list[dict[str, Any]],
) -> None:
    """订单进入 paying 前，以当前购物车刷新金额与快照（draft/confirmed 阶段允许）。"""
    order.total = round(float(total), 2)
    order.items = items
    _save(order)