"""支付前 offer 实时回源校验（P1-1，2026-11-14）。

现状缺口
--------
价格与库存是「检索时点」的快照；从检索到真正扣 PayPal 之间可能隔了十几分钟
（用户来回比价、等团购成团），商家可能已经改价/缺货。这个窗口里没有任何兜底：
检索后即使商品下架，create_order 也照样按旧快照创建 PayPal 订单。

本模块做什么
------------
在 /paypal/create_order 建立 PayPal 订单之前，对购物车里的 Channel3 商品按
offer_url 实时回源一次（SDK products.lookup），然后：
  - 缺货/下架（availability != InStock）→ 拦截，要求回方案页重新选型；
  - 单件涨幅 > max_single_rise（默认 10%）或整篮涨幅 > max_basket_rise（默认 5%）
    → 拦截，要求重新确认（拦截只发生在支付时刻，不破坏人在回路——用户仍可
    「改到可接受再付」）；
  - 全部 OK → 放行，并留 offer_revalidated 审计证据。

设计要点
--------
  - 纯逻辑 + fetch 注入：本模块不直接碰网络，fetch(url) -> (price, availability)
    由调用方传入（生产用 Channel3SearchClient.lookup_product_offer，测试传 fake）。
  - 只校验 source == "channel3" 且带 offer_url 的商品：本地目录条目无商家 URL，
    无从回源，跳过（不误伤也不假装核实过）。
  - fetch 失败不在这里吞：上抛由调用方决定 fail-closed（默认拦截）还是放行。
  - 篮子差异的计算口径：非 Channel3 条目按快照价计入两侧 → 篮子可比。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# fetch 契约：输入商家 URL → (最新单价, 可用性字符串)，失败抛异常
OfferFetcher = Callable[[str], Awaitable[tuple[float, str]]]

REASON_OK = "ok"
REASON_UNAVAILABLE = "unavailable"
REASON_PRICE_CHANGED = "price_changed"


@dataclass
class OfferItemStatus:
    """一件商品的回源对比结果。"""

    id: str
    name: str
    snapshot_price: float
    quantity: int = 1
    live_price: float | None = None
    availability: str = ""
    single_rise_pct: float | None = None
    blocked: bool = False
    reason: str = ""


@dataclass
class RecheckResult:
    ok: bool
    reason: str = REASON_OK
    message: str = ""
    items: list[OfferItemStatus] = field(default_factory=list)
    snapshot_total: float = 0.0
    live_total: float = 0.0
    basket_rise_pct: float | None = None


def _in_stock(availability: str) -> bool:
    """可用性判断：SDK 枚举是 InStock/OutOfStock；空值视为不可知 → 保守按不可售。"""
    a = (availability or "").strip().lower()
    return a in ("", "instock", "in stock", "available")


def _as_dict(item: Any) -> dict[str, Any]:
    """兼容 dict 或带 to_dict() 的对象（PlanItem）。"""
    if isinstance(item, dict):
        return item
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    raise TypeError(f"item 必须是 dict 或带 to_dict() 的对象，实际 {type(item).__name__}")
async def recheck_cart_items(
    cart_items: list[Any],
    *,
    fetch: OfferFetcher,
    max_single_rise: float = 0.10,
    max_basket_rise: float = 0.05,
) -> RecheckResult:
    """对购物车里的 Channel3 商品做实时回源校验，返回 RecheckResult。

    任一商品 fetch 失败 → 抛异常（由调用方决定 fail-closed / fail-open），
    绝不在这里把「不知道」悄悄当成「没涨价」。
    """
    statuses: list[OfferItemStatus] = []
    snapshot_checked_total = 0.0   # 被回源商品（Channel3）的快照小计
    live_checked_total = 0.0       # 被回源商品（Channel3）的实时小计
    snapshot_other_total = 0.0     # 非 Channel3 商品：按快照价计入两侧

    price_changed_items: list[OfferItemStatus] = []
    unavailable_items: list[OfferItemStatus] = []

    for raw in cart_items:
        item = _as_dict(raw)
        source = str(item.get("source") or "")
        offer_url = str(item.get("offer_url") or "").strip()
        quantity = int(item.get("quantity") or 1)
        snapshot_price = float(item.get("price") or 0.0)

        if source != "channel3" or not offer_url:
            # 本地目录/无商家 URL：无从回源。快照价计入两侧保证篮子口径一致。
            snapshot_other_total += snapshot_price * quantity
            continue

        live_price, availability = await fetch(offer_url)
        status = OfferItemStatus(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            snapshot_price=snapshot_price,
            quantity=quantity,
            live_price=live_price,
            availability=availability,
        )
        snapshot_checked_total += snapshot_price * quantity
        live_checked_total += live_price * quantity

        blocked = False
        detail = ""
        if not _in_stock(availability):
            blocked, detail = True, REASON_UNAVAILABLE
            unavailable_items.append(status)
        elif snapshot_price > 0:
            rise = (live_price - snapshot_price) / snapshot_price
            status.single_rise_pct = round(rise, 4)
            if rise > max_single_rise:
                blocked, detail = True, REASON_PRICE_CHANGED
                price_changed_items.append(status)
        status.blocked = blocked
        status.reason = detail
        statuses.append(status)

    snapshot_total = snapshot_checked_total + snapshot_other_total
    live_total = live_checked_total + snapshot_other_total

    basket_rise_pct: float | None = None
    if snapshot_total > 0:
        basket_rise_pct = round((live_total - snapshot_total) / snapshot_total, 4)

    # 拦截判定：缺货 > 单件超阈值 > 整篮超阈值（三者任一即拦，优先级从上到下）
    if unavailable_items:
        names = "、".join(s.name for s in unavailable_items)
        return RecheckResult(
            ok=False,
            reason=REASON_UNAVAILABLE,
            message=f"🚫 支付前校验：{names} 已缺货/下架。请回方案页重新选型后再下单。",
            items=statuses,
            snapshot_total=snapshot_total,
            live_total=live_total,
            basket_rise_pct=basket_rise_pct,
        )
    if price_changed_items:
        details = "、".join(
            f"{s.name} {s.snapshot_price:.2f}→{s.live_price:.2f}"
            for s in price_changed_items
        )
        return RecheckResult(
            ok=False,
            reason=REASON_PRICE_CHANGED,
            message=(
                f"🚨 支付前校验：商品价格已变动（{details}），涨幅超阈值"
                f"（单件>{max_single_rise:.0%} / 整篮>{max_basket_rise:.0%}）。"
                f"请确认新价格后重新提交。"
            ),
            items=statuses,
            snapshot_total=snapshot_total,
            live_total=live_total,
            basket_rise_pct=basket_rise_pct,
        )
    if basket_rise_pct is not None and basket_rise_pct > max_basket_rise:
        return RecheckResult(
            ok=False,
            reason=REASON_PRICE_CHANGED,
            message=(
                f"🚨 支付前校验：整篮价格涨幅 {basket_rise_pct:.1%} 超阈值"
                f"（>{max_basket_rise:.0%}）。请确认新总价后重新提交。"
            ),
            items=statuses,
            snapshot_total=snapshot_total,
            live_total=live_total,
            basket_rise_pct=basket_rise_pct,
        )

    return RecheckResult(
        ok=True,
        reason=REASON_OK,
        message=f"支付前 offer 校验通过：{len(statuses)} 件 Channel3 商品回源一致"
        f"（快照 ${snapshot_total:.2f} / 实时 ${live_total:.2f}）。",
        items=statuses,
        snapshot_total=snapshot_total,
        live_total=live_total,
        basket_rise_pct=basket_rise_pct,
    )