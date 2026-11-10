"""订单支付窗口（惰性过期）回归。

三条契约：
  1. 读路径惰性判定：paying 且越过 expires_at → expired，写一条 order_status_changed
     （actor=lazy_expiry），且幂等（重复读不重复记账）；expired 不可取消。
  2. 支付窗口在 begin_payment 时开启；重复调用不「续命」；短 TTL 由 env 覆盖。
  3. 外部事实优先：本地 expired + PayPal 回跳 COMPLETED → paid，
     审计 reason 必须写明「以支付事实为准」。

为什么是惰性判定而不是定时器：见 README「订单超时：为什么不用 Redis TTL」。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from app.api.paypal_callback import _PENDING_ORDERS, paypal_callback
from app.services.group_purchase import order_lifecycle as ol
from app.services.group_purchase.audit_log import get_audit_logger


class _FakeRequest:
    """最小化 Request 伪造：只要 Accept 头，脚本调用不走 HTML 跳转。"""

    def __init__(self, accept: str = "application/json") -> None:
        self.headers = {"accept": accept}


class _FakeGateway:
    """伪 PayPal 网关：capture_order 返回可控金额与状态。"""

    def __init__(self, captured_amount: float, status: str = "COMPLETED") -> None:
        self.captured_amount = captured_amount
        self.status = status

    def capture_order(self, order_id: str) -> dict[str, Any]:
        return {
            "id": order_id,
            "status": self.status,
            "amount": self.captured_amount,
            "seller_protection": "ELIGIBLE",
        }


def _build_paying_order(session_id: str, total: float = 100.00) -> ol.OrderRecord:
    """直接构造 paying 订单（跳过复核/预算链路，聚焦过期判定本身）。"""
    order = ol.create_order_record(
        session_id=session_id,
        user_id="expiry-tester",
        total=total,
        items=[{"name": "ECV Demo", "quantity": 1, "unit_price": total}],
    )
    order.status = ol.OrderStatus.PAYING
    ol._save(order)
    return order


def _make_stale(order: ol.OrderRecord, seconds_ago: int = 120) -> None:
    """把支付窗口挪到过去，模拟「用户点了支付就去接电话了」。"""
    order.expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat()
    ol._save(order)


def _status_events(session_id: str, order_id: str) -> list[Any]:
    entries = sorted(
        get_audit_logger().get_session_timeline(session_id), key=lambda e: e.timestamp
    )
    return [
        e
        for e in entries
        if e.event == "order_status_changed" and e.data.get("order_id") == order_id
    ]


def _expired_events(session_id: str, order_id: str) -> list[Any]:
    return [
        e for e in _status_events(session_id, order_id) if e.data.get("to_status") == "expired"
    ]



# ============================================================
# 1. 读路径惰性过期 + 审计 + 幂等 + 不可取消
# ============================================================

def test_read_path_expires_stale_paying_order() -> None:
    session_id = "sess-expiry-read"
    order = _build_paying_order(session_id)
    _make_stale(order)

    updated = ol.get_order_record(order.order_id)
    assert updated is not None
    assert updated.status == ol.OrderStatus.EXPIRED

    events = _expired_events(session_id, order.order_id)
    assert len(events) == 1, f"expired 事件应只写 1 条，实际 {len(events)}"
    assert events[0].data["from_status"] == "paying"
    assert events[0].actor == "lazy_expiry"
    assert "支付窗口已过" in events[0].data["reason"]

    # 幂等：再读一次（列表 + 详情）不重复记账
    ol.list_order_records(session_id=session_id)
    ol.get_order_record(order.order_id)
    assert len(_expired_events(session_id, order.order_id)) == 1

    # 过期是终态侧：不可取消（只能被支付事实推翻为 paid）
    ok, msg = ol.cancel_order(updated, actor="user")
    assert ok is False and "expired" in msg
    assert updated.status == ol.OrderStatus.EXPIRED

    # 列表接口把 expired + expires_at 呈现出去（档案页/轮询依赖）
    from app.api import orders as orders_api

    payload = asyncio.run(orders_api.list_orders(session_id=session_id))
    brief = next(o for o in payload["orders"] if o["order_id"] == order.order_id)
    assert brief["status"] == "expired"
    assert brief["expires_at"] == updated.expires_at


# ============================================================
# 2. 未超时不动 / 开窗不续命 / 短 TTL 由 env 覆盖
# ============================================================

def test_paying_window_is_opened_once_and_not_extended(monkeypatch) -> None:
    monkeypatch.setenv("ORDER_PAYING_TTL_SECONDS", "120")
    session_id = "sess-expiry-window"

    order = ol.create_order_record(
        session_id=session_id,
        user_id="expiry-tester",
        total=100.00,
        items=[{"name": "ECV Demo", "quantity": 1, "unit_price": 100.00}],
    )
    # confirmed 才允许 → paying（draft→paying 不在转移表里）
    order.status = ol.OrderStatus.CONFIRMED
    ol._save(order)

    ok, _ = ol.begin_payment(order, actor="tester")
    assert ok and order.status == ol.OrderStatus.PAYING
    assert order.expires_at is not None

    deadline = datetime.fromisoformat(order.expires_at)
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    assert 100 <= remaining <= 140, f"窗口应≈120s，实际 {remaining:.1f}s"

    # 幂等：再次 begin_payment 不延长窗口（否则重试即续命）
    first_deadline = order.expires_at
    ok2, _ = ol.begin_payment(order, actor="tester")
    assert ok2 is True
    assert order.expires_at == first_deadline

    # 未超时 → 读路径不改状态、不写 expired 事件
    ol.get_order_record(order.order_id)
    assert order.status == ol.OrderStatus.PAYING
    assert _expired_events(session_id, order.order_id) == []


# ============================================================
# 3. 外部事实优先：本地 expired + PayPal COMPLETED → paid
# ============================================================

def test_external_payment_fact_overrides_local_expiry(monkeypatch) -> None:
    session_id = "sess-expiry-callback"
    total = 88.00
    order = _build_paying_order(session_id, total=total)
    token = f"TOKEN-{order.order_id}"
    order.paypal_token = token
    _make_stale(order)

    _PENDING_ORDERS[token] = {
        "session_id": session_id,
        "user_email": order.user_id,
        "local_order_id": order.order_id,
    }

    import app.api.paypal_callback as cb

    monkeypatch.setattr(cb, "get_paypal_gateway", lambda: _FakeGateway(total))

    try:
        resp = asyncio.run(
            paypal_callback(
                request=_FakeRequest(),
                token=token,
                PayerID="PAYER-EXPIRY",
                session_id=session_id,
            )
        )
    finally:
        _PENDING_ORDERS.pop(token, None)

    assert resp.get("success") is True, resp
    assert resp.get("status") == "COMPLETED", resp

    updated = ol.get_order_record(order.order_id)
    assert updated is not None and updated.status == ol.OrderStatus.PAID

    events = _status_events(session_id, order.order_id)
    assert [e.data["to_status"] for e in events] == ["expired", "paid"]
    assert "以支付事实为准" in events[-1].data["reason"]
    # 机器可读原因码 + 金额证据：Q&A 可 grep 审计流水直接复算「本地推断被外部事实推翻」
    assert events[-1].data["reason_code"] == "remote_completed_after_local_expiry"
    assert updated.transitions[-1]["reason_code"] == "remote_completed_after_local_expiry"
    assert f"{total:.2f}" in events[-1].data["reason"], events[-1].data["reason"]


# ============================================================
# 4. 过期后重试支付：不复用过期单，新建订单（旧单留档为过期证据）
# ============================================================

def test_retry_after_expiry_creates_a_new_order() -> None:
    from app.api.paypal_callback import _get_or_create_order
    from app.services.group_purchase.session_state import get_or_create_session

    session_id = "sess-expiry-retry"
    session = get_or_create_session(session_id, "expiry-tester")

    order = _build_paying_order(session_id, total=50.00)
    session.order_id = order.order_id
    _make_stale(order)
    expired = ol.get_order_record(order.order_id)
    assert expired is not None and expired.status == ol.OrderStatus.EXPIRED

    fresh = _get_or_create_order(
        session,
        "expiry-tester",
        50.00,
        [{"name": "ECV Demo", "quantity": 1, "unit_price": 50.00}],
    )
    assert fresh.order_id != order.order_id, "过期订单不应被复用（会撞上 expired→paying 非法转移）"
    assert fresh.status == ol.OrderStatus.DRAFT
    # 旧单仍在档案里，作为「确实过期过」的证据
    assert ol.get_order_record(order.order_id).status == ol.OrderStatus.EXPIRED
