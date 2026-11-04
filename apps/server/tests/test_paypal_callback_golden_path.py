"""
Golden path 验证脚本：PayPal 回调审计事件顺序。

验证内容：
 1. 正常路径（golden path）：capture 成功 → 写 payment_completed → complete_payment(paying→paid)，
     审计时间线严格匹配预期的 8 个事件顺序，且 payment_completed 在 order_status_changed(paying→paid) 之前。
 2. 异常路径：D2 副门金额不匹配（captured_amount != order.total）→ 写独立的 payment_amount_mismatch 告警，
     订单落 cancelled，不写 payment_completed。

运行：python tests/test_paypal_callback_golden_path.py
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.api.paypal_callback import (
    _PENDING_ORDERS,
    paypal_callback,
)
from app.core.config import get_settings
from app.services.group_purchase.audit_log import get_audit_logger
from app.services.group_purchase.order_lifecycle import (
    OrderStatus,
    create_order_record,
    get_order_record,
)


class _FakeRequest:
    """最小化的 Request 伪造：只暴露 headers，脚本调用不走 text/html 跳转。"""

    def __init__(self, accept: str = "application/json") -> None:
        self.headers = {"accept": accept}


class _FakeGateway:
    """伪 PayPal 网关：capture_order 返回可控的 captured_amount / status。"""

    def __init__(self, captured_amount: float = 100.00, status: str = "COMPLETED") -> None:
        self.captured_amount = captured_amount
        self.status = status

    def capture_order(self, order_id: str) -> dict[str, Any]:
        return {
            "id": order_id,
            "status": self.status,
            "amount": self.captured_amount,
            "seller_protection": "ELIGIBLE",
        }


def _build_paying_order(session_id: str, total: float = 100.00, user_id: str = "demo-user"):
    """创建一个处于 PAYING 状态的订单，并绑定 paypal_token，返回 (order, token)。"""
    order = create_order_record(
        session_id=session_id,
        user_id=user_id,
        total=total,
        items=[{"name": "ECV Demo", "quantity": 1, "unit_price": total}],
    )
    # 直接把订单推到 paying（跳过 review_confirmed/begin_payment，聚焦验证回调审计顺序）。
    order.status = OrderStatus.PAYING
    token = f"TOKEN-{order.order_id}"
    order.paypal_token = token
    from app.services.group_purchase.order_lifecycle import _save

    _save(order)
    return order, token


def _event_sequence(logger, session_id: str) -> list[str]:
    entries = logger.get_session_timeline(session_id)
    # get_session_timeline 内部按 timestamp 逆序（reverse=True）返回；这里按时间戳升序还原真实写入顺序。
    entries = sorted(entries, key=lambda e: e.timestamp)
    return [e.event for e in entries]


async def test_golden_path_normal() -> None:
    """正常路径：8 个审计事件严格匹配预期序列。"""
    logger = get_audit_logger()
    session_id = "gp-normal-session"
    total = 100.00

    order, token = _build_paying_order(session_id, total=total)

    # 登记待处理订单（回调侧按 token 还原 session / local_order_id）。
    _PENDING_ORDERS[token] = {
        "session_id": session_id,
        "user_email": order.user_id,
        "local_order_id": order.order_id,
    }

    # Monkeypatch 网关：capture 成功，金额与订单一致。
    import app.api.paypal_callback as cb_module

    orig_gateway = cb_module.get_paypal_gateway
    cb_module.get_paypal_gateway = lambda: _FakeGateway(captured_amount=total, status="COMPLETED")

    try:
        resp = await paypal_callback(
            request=_FakeRequest(),
            token=token,
            PayerID="PAYER-1",
            session_id=session_id,
        )
    finally:
        cb_module.get_paypal_gateway = orig_gateway

    assert resp.get("success") is True, resp
    assert resp.get("status") == "COMPLETED", resp

    # 订单应已转 paid。
    updated = get_order_record(order.order_id)
    assert updated.status == OrderStatus.PAID, updated.status

    # 审计时间线：严格匹配预期序列。
    seq = _event_sequence(logger, session_id)
    expected = [
        "payment_completed",
        "order_status_changed",
    ]
    # 注意：同一会话里之前可能已有 order_status_changed(confirmed→paying) 等事件；
    # 这里只断言“最后两条”严格为 payment_completed → order_status_changed(paying→paid)。
    assert seq[-2:] == expected, f"审计尾部序列不匹配: {seq[-4:]} != {expected}"

    # payment_completed 必须在 order_status_changed(paying→paid) 之前（时间戳递增）。
    entries = logger.get_session_timeline(session_id)
    pc_entries = [e for e in entries if e.event == "payment_completed" and e.data.get("order_id") == order.order_id]
    os_entries = [
        e
        for e in entries
        if e.event == "order_status_changed"
        and e.data.get("order_id") == order.order_id
        and e.data.get("to_status") == "paid"
    ]
    assert len(pc_entries) == 1, f"payment_completed 应仅写 1 条，实际 {len(pc_entries)}"
    assert len(os_entries) == 1, f"order_status_changed(paying→paid) 应仅写 1 条，实际 {len(os_entries)}"
    assert pc_entries[0].timestamp < os_entries[0].timestamp, (
        "payment_completed 必须先于 order_status_changed(paying→paid)"
    )

    print("[GOLDEN PATH] 正常路径通过：payment_completed → order_status_changed(paying→paid)，顺序正确，幂等计数可信。")


async def test_golden_path_amount_mismatch() -> None:
    """异常路径：D2 副门金额不匹配 → payment_amount_mismatch 告警 + cancelled，不写 payment_completed。"""
    logger = get_audit_logger()
    session_id = "gp-mismatch-session"
    total = 100.00
    captured = 9999.00  # 故意不匹配

    order, token = _build_paying_order(session_id, total=total)

    _PENDING_ORDERS[token] = {
        "session_id": session_id,
        "user_email": order.user_id,
        "local_order_id": order.order_id,
    }

    import app.api.paypal_callback as cb_module

    orig_gateway = cb_module.get_paypal_gateway
    cb_module.get_paypal_gateway = lambda: _FakeGateway(captured_amount=captured, status="COMPLETED")

    try:
        resp = await paypal_callback(
            request=_FakeRequest(),
            token=token,
            PayerID="PAYER-2",
            session_id=session_id,
        )
    finally:
        cb_module.get_paypal_gateway = orig_gateway

    assert resp.get("success") is False, resp
    assert resp.get("status") == "CANCELLED", resp
    assert resp.get("reason") == "amount_mismatch", resp

    # 订单应已转 cancelled。
    updated = get_order_record(order.order_id)
    assert updated.status == OrderStatus.CANCELLED, updated.status

    # 审计：必须有 payment_amount_mismatch，且带 captured_amount / expected_amount / paypal_token。
    entries = logger.get_session_timeline(session_id)
    mismatch_entries = [
        e
        for e in entries
        if e.event == "payment_amount_mismatch" and e.data.get("order_id") == order.order_id
    ]
    assert len(mismatch_entries) == 1, f"payment_amount_mismatch 应仅写 1 条，实际 {len(mismatch_entries)}"
    data = mismatch_entries[0].data
    assert abs(float(data["captured_amount"]) - captured) < 1e-6, data
    assert abs(float(data["expected_amount"]) - total) < 1e-6, data
    assert data["paypal_token"] == token, data

    # 关键：不得写 payment_completed。
    pc_entries = [e for e in entries if e.event == "payment_completed" and e.data.get("order_id") == order.order_id]
    assert len(pc_entries) == 0, f"金额不匹配时不得写 payment_completed，实际 {len(pc_entries)}"

    print(
        "[GOLDEN PATH] 异常路径通过：payment_amount_mismatch 告警落盘 + 订单 cancelled，未写 payment_completed。"
    )


async def main() -> None:
    await test_golden_path_normal()
    await test_golden_path_amount_mismatch()
    print("\n全部 Golden Path 验证通过。")


if __name__ == "__main__":
    asyncio.run(main())