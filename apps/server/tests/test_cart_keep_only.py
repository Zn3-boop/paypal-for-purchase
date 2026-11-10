"""改单引擎：「只要 X」的确定性编译（多件保留 + 数量）回归。

契约（判据都在这里，不靠模型发挥）：
  - 「只需要 X」= 保留 X，其余每一件都删；总价 = X 的单价 × 数量；
  - 「只要 A 和 B」= 多个不同种类一起保留，不误删点名的；
  - 「只要 2 个 C」= 同一件保留但数量置 2，同款重复行合并成一行；
  - 只认购物车里的真实商品：一件都认不出 → success=False 并回显购物车现有商品，
    绝不在认不出时把车删空；
  - 购物车本来就只有这些件 → success=True（"已满足"），不报"没识别到改动"。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase.cart_modifier import modify_cart_and_replan
from app.services.group_purchase.session_state import (
    PlanItem,
    get_or_create_session,
    update_session,
)


DJ = PlanItem(id="ch3_dj", name="DJ 300 PRO X DJ Headphones", category="DJ Headphones", price=159.99)
HD = PlanItem(id="ch3_hd", name="HD 300", category="On-Ear Headphones", price=39.95)
AVENTHO = PlanItem(id="ch3_av", name="AVENTHO 300", category="Over-Ear Headphones", price=399.0)
KOSS = PlanItem(id="ch3_koss", name="Koss Kph7hb Wired On Ear Portable Headphones",
                category="On-Ear Headphones", price=5.49)


def _session(session_id: str, items: list[PlanItem]):
    session = get_or_create_session(session_id, "user-keep")
    session.cart_items = [PlanItem(**it.to_dict()) for it in items]
    session.state = "plan_generated"
    update_session(session)
    return session


def _modify(session_id: str, instruction: str) -> dict:
    return asyncio.run(
        modify_cart_and_replan(
            session_id=session_id,
            user_id="user-keep",
            user_instruction=instruction,
            catalog=[],
        )
    )


def _names(out: dict) -> list[str]:
    return [it["name"] for it in out["updated_cart"]]


def test_keep_only_single_item_drops_the_rest():
    _session("keep-single", [DJ, HD, AVENTHO, KOSS])

    out = _modify("keep-single", "只需要 DJ 300 PRO X DJ Headphones")

    assert out["success"] is True
    assert _names(out) == [DJ.name]
    assert out["new_total"] == 159.99
    assert "保留 1 件" in out["intent_summary"]


def test_keep_only_multiple_categories_keeps_every_named_item():
    _session("keep-multi", [DJ, HD, AVENTHO, KOSS])

    out = _modify("keep-multi", "只要 DJ 300 PRO X DJ Headphones 和 AVENTHO 300")

    assert out["success"] is True
    assert set(_names(out)) == {DJ.name, AVENTHO.name}
    assert out["new_total"] == round(159.99 + 399.0, 2)


def test_keep_only_does_not_pull_in_a_longer_title_variant():
    """说「AVENTHO 300」不应把「AVENTHO 300 Wireless…」也一起留下。"""
    longer = PlanItem(id="ch3_av2", name="AVENTHO 300 Wireless Bluetooth Headphones",
                      category="Over-Ear Headphones", price=299.99)
    _session("keep-variant", [DJ, AVENTHO, longer])

    out = _modify("keep-variant", "只要 AVENTHO 300")

    assert out["success"] is True
    assert _names(out) == ["AVENTHO 300"]


def test_keep_only_sets_quantity_and_merges_duplicate_lines():
    _session("keep-qty", [HD, HD, DJ])

    out = _modify("keep-qty", "只要 2 个 HD 300")

    assert out["success"] is True
    assert _names(out) == [HD.name]
    item = out["updated_cart"][0]
    assert item["quantity"] == 2
    assert item["subtotal"] == round(39.95 * 2, 2)
    assert out["new_total"] == round(39.95 * 2, 2)


def test_keep_only_unknown_item_never_empties_the_cart():
    _session("keep-unknown", [DJ, HD])

    out = _modify("keep-unknown", "只要 索尼 WH-1000XM5")

    assert out["success"] is False
    assert "没能从这句话里认出" in out["message"]
    assert set(_names(out)) == {DJ.name, HD.name}


def test_keep_only_when_already_satisfied_is_not_reported_as_noop_failure():
    _session("keep-satisfied", [DJ])

    out = _modify("keep-satisfied", "只需要 DJ 300 PRO X DJ Headphones")

    assert out["success"] is True
    assert "已经是" in out["message"]
    assert _names(out) == [DJ.name]


def test_delete_instruction_still_works():
    """回归：非「只要」句式仍走原删除路径。"""
    _session("keep-delete", [DJ, HD])

    out = _modify("keep-delete", "删掉 HD 300")

    assert out["success"] is True
    assert _names(out) == [DJ.name]
