"""P1-1 支付前 offer 实时回源校验（2026-11-14）。

契约（对标「缺货/改价窗口无兜底」硬伤）：
  1. 纯逻辑 recheck_cart_items：
     - 缺货/下架（availability != InStock）→ 拦截（unavailable）
     - 单件涨幅 > 阈值（默认 10%）→ 拦截（price_changed）
     - 整篮涨幅 > 阈值（默认 5%）→ 拦截（price_changed）
     - 全部一致 → 放行，basket_rise_pct=0，留审计证据
     - 非 Channel3 / 无 offer_url 的商品不 fetch、不误伤
     - fetch 失败必须上抛（由调用方决定 fail-closed / fail-open，不悄悄放行）
  2. 集成（/paypal/create_order + _enforce_offer_recheck）：
     - 开启配置后缺货 → 400 + offer_unavailable 审计
     - 开启配置后涨价 → 400 + offer_price_changed 审计（含单件/整篮明细）
     - 回源失败 fail-closed → 400 + offer_recheck_failed 审计
     - 回源通过 → 正常建 PayPal 订单 + offer_revalidated 审计
     - 配置关闭 → 旧行为完全不变（不触发回源、不写新审计）
  3. 浏览器/脚本调用都能拿到机器可读 status（detail.status），前端按 message 展示。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.channel3.offer_recheck import (
    REASON_OK,
    REASON_PRICE_CHANGED,
    REASON_UNAVAILABLE,
    recheck_cart_items,
)


def _item(item_id: str, name: str, price: float, *, qty: int = 1,
          source: str = "channel3", offer_url: str | None = None) -> dict[str, Any]:
    """构造购物车条目；offer_url=None 时给默认商家 URL，显式传空串则保持为空。"""
    return {
        "id": item_id,
        "name": name,
        "category": "鞋",
        "price": price,
        "quantity": qty,
        "source": source,
        "offer_url": offer_url if offer_url is not None else f"https://shop.example.com/{item_id}",
    }


def _items() -> list[dict[str, Any]]:
    return [_item("p1", "运动鞋", 100.0), _item("p2", "帆布袋", 40.0)]


class _FakeFetch:
    """可控回源：按 URL 返回 (price, availability)，可注入失败。"""

    def __init__(self, prices: dict[str, tuple[float, str]] | None = None,
                 raise_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.prices = prices or {}
        self.raise_on = raise_on

    async def __call__(self, url: str) -> tuple[float, str]:
        self.calls.append(url)
        if self.raise_on is not None and url.startswith(self.raise_on):
            raise ConnectionError(f"lookup failed for {url}")
        return self.prices.get(url, (0.0, "InStock"))


# ---------------------------------------------------------------- 纯逻辑

def test_recheck_pass_with_identical_prices() -> None:
    result = asyncio.run(
        recheck_cart_items(
            _items(),
            fetch=_FakeFetch({"https://shop.example.com/p1": (100.0, "InStock"),
                              "https://shop.example.com/p2": (40.0, "InStock")}),
        )
    )
    assert result.ok is True
    assert result.reason == REASON_OK
    assert result.basket_rise_pct == 0.0
    assert result.snapshot_total == 140.0 and result.live_total == 140.0


def test_recheck_blocks_out_of_stock() -> None:
    result = asyncio.run(
        recheck_cart_items(
            _items(),
            fetch=_FakeFetch({"https://shop.example.com/p1": (100.0, "OutOfStock"),
                              "https://shop.example.com/p2": (40.0, "InStock")}),
        )
    )
    assert result.ok is False
    assert result.reason == REASON_UNAVAILABLE
    blocked = [s for s in result.items if s.blocked]
    assert [s.id for s in blocked] == ["p1"]
    assert "缺货" in result.message


def test_recheck_blocks_single_rise_over_threshold() -> None:
    # +15% 单件涨幅 > 10%
    result = asyncio.run(
        recheck_cart_items(
            _items(),
            fetch=_FakeFetch({"https://shop.example.com/p1": (115.0, "InStock"),
                              "https://shop.example.com/p2": (40.0, "InStock")}),
        )
    )
    assert result.ok is False
    assert result.reason == REASON_PRICE_CHANGED
    p1 = next(s for s in result.items if s.id == "p1")
    assert p1.single_rise_pct == pytest.approx(0.15)
    assert p1.blocked is True
    assert "价格已变动" in result.message


def test_recheck_allows_small_rise_below_single_threshold() -> None:
    # 单件 +4%、整篮约 +2.9% 都低于阈值 → 放行
    result = asyncio.run(
        recheck_cart_items(
            _items(),
            fetch=_FakeFetch({"https://shop.example.com/p1": (104.0, "InStock"),
                              "https://shop.example.com/p2": (40.0, "InStock")}),
        )
    )
    assert result.ok is True
    assert result.basket_rise_pct == pytest.approx(0.0286, rel=1e-3)


def test_recheck_blocks_basket_rise_over_threshold() -> None:
    # 两件各 +6%：单件都在 10% 以内，但整篮 +6% > 5% → 篮级拦截
    result = asyncio.run(
        recheck_cart_items(
            _items(),
            fetch=_FakeFetch({"https://shop.example.com/p1": (106.0, "InStock"),
                              "https://shop.example.com/p2": (42.4, "InStock")}),
        )
    )
    assert result.ok is False
    assert result.reason == REASON_PRICE_CHANGED
    assert result.basket_rise_pct == pytest.approx(0.06)
    assert "整篮" in result.message


def test_recheck_skips_local_catalog_items_and_respects_basket_pool() -> None:
    cart = [
        _item("p1", "运动鞋", 100.0),  # channel3：回源 +4%（篮子 4/130≈3%<5%）
        _item("local1", "本地雨伞", 30.0, source="local_catalog", offer_url=""),
    ]
    fetch = _FakeFetch({"https://shop.example.com/p1": (104.0, "InStock")})
    result = asyncio.run(recheck_cart_items(cart, fetch=fetch))
    assert len(fetch.calls) == 1, "本地目录条目不应该被回源"
    assert result.ok is True
    # 篮子口径：本地件按快照价计入两侧
    assert result.snapshot_total == 130.0
    assert result.live_total == 134.0


def test_recheck_fetch_failure_raises_not_swallowed() -> None:
    cart = [_item("p1", "运动鞋", 100.0)]
    with pytest.raises(ConnectionError):
        asyncio.run(recheck_cart_items(cart, fetch=_FakeFetch(raise_on="https://shop")))


def test_recheck_zero_snapshot_price_skips_price_compare() -> None:
    cart = [_item("p1", "运动鞋", 0.0)]
    ok = asyncio.run(
        recheck_cart_items(cart, fetch=_FakeFetch({"https://shop.example.com/p1": (99.0, "InStock")}))
    )
    assert ok.ok is True and ok.basket_rise_pct is None
    bad = asyncio.run(
        recheck_cart_items(cart, fetch=_FakeFetch({"https://shop.example.com/p1": (99.0, "OutOfStock")}))
    )
    assert bad.ok is False and bad.reason == REASON_UNAVAILABLE
# ---------------------------------------------------------------- 集成

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _seed_cart(sid: str, cart_items: list[dict[str, Any]], user: str = "recheck-tester") -> None:
    """直接种会话购物车（不依赖 Channel3 网络），PlanItem 语义与 cart_api 一致。"""
    from app.services.group_purchase.session_state import (
        PlanItem,
        get_or_create_session,
        update_session,
    )

    session = get_or_create_session(sid, user)
    session.cart_items = [PlanItem.from_dict(i) for i in cart_items]
    update_session(session)


def _audit_events(sid: str) -> list[dict[str, Any]]:
    from app.services.group_purchase.audit_log import get_audit_logger

    return [e.to_dict() for e in get_audit_logger().get_session_timeline(sid)]


class _FakeGateway:
    """create_order 不触网：返回可控 approval_url（与真实 gateway 同步契约一致）。"""

    def __init__(self) -> None:
        self.calls = 0

    def create_order(self, *args, **kwargs):
        self.calls += 1
        return {
            "order_id": "ORDER-MOCK-1",
            "status": "CREATED",
            "approval_url": "https://sandbox.approve/ORDER-MOCK-1",
            "links": [{"rel": "payer-action", "href": "https://sandbox.approve/ORDER-MOCK-1"}],
        }


class _FakeChannel3Client:
    """lookup_product_offer 可控：按 URL 返回 (price, availability)，可注入失败。"""

    def __init__(self, prices: dict[str, tuple[float, str]], fail: bool = False) -> None:
        self.prices = prices
        self.fail = fail

    async def lookup_product_offer(self, offer_url: str, timeout: float = 20.0):
        if self.fail:
            raise ConnectionError("realtime lookup failed")
        return self.prices.get(offer_url, (0.0, "InStock"))


def _enable_recheck(monkeypatch, *, fail_open: bool = False) -> None:
    monkeypatch.setenv("CHANNEL3_OFFER_RECHECK_ENABLED", "true")
    monkeypatch.setenv("CHANNEL3_OFFER_RECHECK_FAIL_OPEN", "true" if fail_open else "false")


def _call_create_order(client, sid: str) -> Any:
    return client.post(
        "/api/v1/paypal/create_order",
        params={"session_id": sid},
        json={
            "amount": 999.0,  # 前端传的 amount 不应被信任，服务端用购物车价
            "session_id": sid,
            "user_email": "recheck-tester@example.com",
            "item_name": "ECV Recheck Demo",
        },
    )


def test_create_order_blocked_on_unavailable(monkeypatch, client) -> None:
    sid = "sess-recheck-unavailable"
    cart = [_item("p1", "运动鞋", 100.0, offer_url="https://shop.example.com/p1")]
    _seed_cart(sid, cart)
    _enable_recheck(monkeypatch)

    import app.api.paypal_callback as cb

    monkeypatch.setattr(cb, "get_paypal_gateway", lambda: _FakeGateway())
    monkeypatch.setattr(
        "app.services.channel3.client.get_channel3_client",
        lambda: _FakeChannel3Client({"https://shop.example.com/p1": (100.0, "OutOfStock")}),
    )

    r = _call_create_order(client, sid)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["status"] == "offer_unavailable"
    assert "缺货" in r.json()["detail"]["message"]

    events = _audit_events(sid)
    assert "offer_unavailable" in [e["event"] for e in events]
    assert "offer_revalidated" not in [e["event"] for e in events]
    # 未创建 PayPal 订单：状态机没有推进到 paying
    ev = next(e for e in events if e["event"] == "offer_unavailable")
    assert ev["data"]["item_ids"] == ["p1"]
    assert ev["data"]["item_names"] == ["运动鞋"]


def test_create_order_blocked_on_price_change(monkeypatch, client) -> None:
    sid = "sess-recheck-price"
    cart = [_item("p1", "运动鞋", 100.0)]
    _seed_cart(sid, cart)
    _enable_recheck(monkeypatch)

    import app.api.paypal_callback as cb

    monkeypatch.setattr(cb, "get_paypal_gateway", lambda: _FakeGateway())
    monkeypatch.setattr(
        "app.services.channel3.client.get_channel3_client",
        lambda: _FakeChannel3Client({"https://shop.example.com/p1": (130.0, "InStock")}),
    )

    r = _call_create_order(client, sid)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "offer_price_changed"
    assert "价格已变动" in detail["message"]

    ev = next(e for e in _audit_events(sid) if e["event"] == "offer_price_changed")
    assert ev["data"]["changes"][0]["snapshot_price"] == 100.0
    assert ev["data"]["changes"][0]["live_price"] == 130.0
    assert ev["data"]["changes"][0]["single_rise_pct"] == pytest.approx(0.3)
def test_create_order_fail_closed_on_recheck_failure(monkeypatch, client) -> None:
    sid = "sess-recheck-failclosed"
    cart = [_item("p1", "运动鞋", 100.0)]
    _seed_cart(sid, cart)
    _enable_recheck(monkeypatch, fail_open=False)

    import app.api.paypal_callback as cb

    gateway = _FakeGateway()
    monkeypatch.setattr(cb, "get_paypal_gateway", lambda: gateway)
    monkeypatch.setattr(
        "app.services.channel3.client.get_channel3_client",
        lambda: _FakeChannel3Client({}, fail=True),
    )

    r = _call_create_order(client, sid)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["status"] == "offer_recheck_failed"
    assert gateway.calls == 0, "回源失败默认 fail-closed：不应创建 PayPal 订单"

    ev = next(e for e in _audit_events(sid) if e["event"] == "offer_recheck_failed")
    assert ev["data"]["fail_open"] is False


def test_create_order_fail_open_proceeds_with_audit(monkeypatch, client) -> None:
    sid = "sess-recheck-failopen"
    cart = [_item("p1", "运动鞋", 100.0)]
    _seed_cart(sid, cart)
    _enable_recheck(monkeypatch, fail_open=True)

    import app.api.paypal_callback as cb

    gateway = _FakeGateway()
    monkeypatch.setattr(cb, "get_paypal_gateway", lambda: gateway)
    monkeypatch.setattr(
        "app.services.channel3.client.get_channel3_client",
        lambda: _FakeChannel3Client({}, fail=True),
    )

    r = _call_create_order(client, sid)
    assert r.status_code == 200, r.text
    assert r.json()["approval_url"]
    assert gateway.calls == 1
    ev = next(e for e in _audit_events(sid) if e["event"] == "offer_recheck_failed")
    assert ev["data"]["fail_open"] is True


def test_create_order_passes_and_logs_revalidated(monkeypatch, client) -> None:
    sid = "sess-recheck-pass"
    cart = [_item("p1", "运动鞋", 100.0)]
    _seed_cart(sid, cart)
    _enable_recheck(monkeypatch)

    import app.api.paypal_callback as cb

    gateway = _FakeGateway()
    monkeypatch.setattr(cb, "get_paypal_gateway", lambda: gateway)
    monkeypatch.setattr(
        "app.services.channel3.client.get_channel3_client",
        lambda: _FakeChannel3Client({"https://shop.example.com/p1": (100.0, "InStock")}),
    )

    r = _call_create_order(client, sid)
    assert r.status_code == 200, r.text
    assert gateway.calls == 1
    ev = next(e for e in _audit_events(sid) if e["event"] == "offer_revalidated")
    assert ev["data"]["item_count"] == 1
    assert ev["data"]["live_total"] == 100.0


def test_create_order_unaffected_when_recheck_disabled(monkeypatch, client) -> None:
    """配置关闭 = 旧行为完全不变：不触发回源、不写 P1-1 审计。"""
    sid = "sess-recheck-off"
    cart = [_item("p1", "运动鞋", 100.0)]
    _seed_cart(sid, cart)
    monkeypatch.delenv("CHANNEL3_OFFER_RECHECK_ENABLED", raising=False)

    import app.api.paypal_callback as cb

    monkeypatch.setattr(cb, "get_paypal_gateway", lambda: _FakeGateway())

    # 即使底层回源会炸，配置关闭也不该被调用
    def boom():
        raise AssertionError("recheck disabled 时不应触发回源")

    monkeypatch.setattr("app.services.channel3.client.get_channel3_client", boom)

    r = _call_create_order(client, sid)
    assert r.status_code == 200, r.text
    events = [e["event"] for e in _audit_events(sid)]
    assert not any(
        e in events for e in ("offer_revalidated", "offer_unavailable",
                              "offer_price_changed", "offer_recheck_failed")
    )