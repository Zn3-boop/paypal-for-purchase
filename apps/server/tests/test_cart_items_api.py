"""购物车可编辑闭环回归 —— PATCH/DELETE + select_plan 的 replace/append 语义。

覆盖录制现场必炸的四个点：

1. **改数量要真的生效**：总价、行小计、落盘会话、审计留痕四处一致
   （"点了 + 却没变"比没有按钮更伤）；
2. **删商品要真的消失**，删不存在的必须 404 —— 静默成功会让用户以为删掉了；
3. **再点方案不能静默清空购物车**：replace / append 语义显式化，并把
   `removed_count` / `added_count` 回显出来（"我手加的东西怎么没了"是致命体验）；
4. **超预算只报告不拦截**：用户得能"改到预算内再付"，把门的是 create_order。
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.session_state import (
    PlanItem,
    ShoppingPlan,
    get_or_create_session,
    update_session,
)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _item(item_id: str, name: str, price: float, qty: int = 1) -> PlanItem:
    return PlanItem(id=item_id, name=name, category="桌游", price=price, quantity=qty)


def _clone(items: list[PlanItem]) -> list[PlanItem]:
    return [PlanItem.from_dict(i.to_dict()) for i in items]


def _seed(sid: str, cart: list[PlanItem], plan_a: list[PlanItem] | None = None,
          budget: float | None = None) -> None:
    """直接种会话：购物车 + 方案 A（+ 可选硬预算），不依赖 Channel3 网络。"""
    session = get_or_create_session(sid, "cart-tester")
    session.cart_items = _clone(cart)
    plan_items = plan_a if plan_a is not None else cart
    session.plan_a = ShoppingPlan(
        plan_id="A",
        strategy="低价优先",
        items=_clone(plan_items),
        total=round(sum(i.price * i.quantity for i in plan_items), 2),
    )
    if budget is not None:
        session.constraints.append(
            Constraint(
                id="c_budget_test",
                type="budget",
                content=f"总预算 ${budget}",
                hard=True,
                source="user",
                numeric_value=budget,
            )
        )
    update_session(session)


def _audit(client, sid: str) -> list[str]:
    r = client.get("/api/v1/audit/logs", params={"session_id": sid, "limit": 500})
    assert r.status_code == 200, r.text
    return [e["event"] for e in r.json()["logs"]]


# ---------------------------------------------------------------- 改数量

def test_patch_quantity_updates_total_subtotal_and_audit(client):
    sid = _sid("cart-patch")
    _seed(sid, [_item("sku1", "环保桌游·森林棋", 25.0), _item("sku2", "有机棉帆布袋", 15.0)])

    r = client.patch(
        "/api/v1/cart/items/sku1", params={"session_id": sid}, json={"quantity": 3}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["quantity"] == 3
    assert body["cart"]["total"] == 90.0, body["cart"]          # 25*3 + 15
    line = next(i for i in body["cart"]["items"] if i["id"] == "sku1")
    assert line["subtotal"] == 75.0, line

    # 不是只在响应里算对：再查一次购物车，数量必须真的落盘
    again = client.get("/api/v1/cart", params={"session_id": sid}).json()
    assert next(i for i in again["items"] if i["id"] == "sku1")["quantity"] == 3
    assert again["total"] == 90.0
    assert "cart_modified" in _audit(client, sid)


def test_patch_unknown_item_is_404(client):
    sid = _sid("cart-patch-404")
    _seed(sid, [_item("sku1", "环保桌游", 25.0)])
    r = client.patch(
        "/api/v1/cart/items/nope", params={"session_id": sid}, json={"quantity": 2}
    )
    assert r.status_code == 404, r.text


def test_patch_zero_quantity_is_rejected_not_silently_deleted(client):
    """0 件必须报错指向 DELETE：否则「手滑减到 0」会静默丢件。"""
    sid = _sid("cart-patch-zero")
    _seed(sid, [_item("sku1", "环保桌游", 25.0)])

    r = client.patch(
        "/api/v1/cart/items/sku1", params={"session_id": sid}, json={"quantity": 0}
    )
    assert r.status_code == 400, r.text
    # 商品还在，数量没被改成 0
    cart = client.get("/api/v1/cart", params={"session_id": sid}).json()
    assert len(cart["items"]) == 1 and cart["items"][0]["quantity"] == 1


def test_patch_merges_duplicate_rows_of_same_item(client):
    """同款拆成两行是数据噪声，PATCH 后必须合并成一行。"""
    sid = _sid("cart-patch-merge")
    _seed(sid, [_item("sku1", "环保桌游", 25.0), _item("sku1", "环保桌游", 25.0)])

    r = client.patch(
        "/api/v1/cart/items/sku1", params={"session_id": sid}, json={"quantity": 4}
    )
    assert r.status_code == 200, r.text
    rows = [i for i in r.json()["cart"]["items"] if i["id"] == "sku1"]
    assert len(rows) == 1 and rows[0]["quantity"] == 4, rows
    assert r.json()["cart"]["total"] == 100.0


# ---------------------------------------------------------------- 删商品

def test_delete_removes_item_and_audits(client):
    sid = _sid("cart-del")
    _seed(sid, [_item("sku1", "环保桌游", 25.0), _item("sku2", "帆布袋", 15.0)])

    r = client.delete("/api/v1/cart/items/sku2", params={"session_id": sid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["remove_count"] == 1
    assert [i["id"] for i in body["cart"]["items"]] == ["sku1"]
    assert body["cart"]["total"] == 25.0
    assert body["new_total"] < body["old_total"]
    assert "cart_modified" in _audit(client, sid)

    cart = client.get("/api/v1/cart", params={"session_id": sid}).json()
    assert [i["id"] for i in cart["items"]] == ["sku1"]


def test_delete_unknown_item_is_404(client):
    sid = _sid("cart-del-404")
    _seed(sid, [_item("sku1", "环保桌游", 25.0)])
    r = client.delete("/api/v1/cart/items/nope", params={"session_id": sid})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------- 选方案语义

def test_select_plan_replace_reports_removed_items(client):
    """手动加购 → 再点方案（replace）：消失的件数必须回显，且写 cart_modified。"""
    sid = _sid("cart-select-replace")
    _seed(
        sid,
        cart=[_item("manual_1", "手动加购的咖啡", 12.0), _item("sku1", "环保桌游", 25.0)],
        plan_a=[_item("sku1", "环保桌游", 25.0), _item("sku3", "竹纤维笔", 8.0)],
    )

    r = client.post(
        "/api/v1/cart/select_plan",
        params={"session_id": sid, "plan_id": "A", "mode": "replace"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "replace"
    assert body["replaced"] is True
    assert body["removed_count"] == 1, body            # manual_1 被替换掉
    assert body["added_count"] == 1, body              # sku3 是新加的
    assert {i["id"] for i in body["cart"]["items"]} == {"sku1", "sku3"}
    assert "cart_modified" in _audit(client, sid)


def test_select_plan_append_merges_quantities_without_losing_user_edits(client):
    """append：已有商品数量相加、新商品追加 —— 用户手改的数量不能丢。"""
    sid = _sid("cart-select-append")
    _seed(
        sid,
        cart=[_item("sku1", "环保桌游", 25.0, qty=2)],
        plan_a=[_item("sku1", "环保桌游", 25.0, qty=1), _item("sku3", "竹纤维笔", 8.0)],
    )

    r = client.post(
        "/api/v1/cart/select_plan",
        params={"session_id": sid, "plan_id": "A", "mode": "append"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "append"
    assert body["replaced"] is False
    assert body["removed_count"] == 0
    assert body["added_count"] == 1, body
    rows = {i["id"]: i for i in body["cart"]["items"]}
    assert rows["sku1"]["quantity"] == 3, rows["sku1"]      # 2 + 1
    assert rows["sku1"]["subtotal"] == 75.0
    assert "sku3" in rows
    assert body["total"] == 83.0, body                     # 75 + 8


def test_select_plan_rejects_unknown_mode(client):
    sid = _sid("cart-select-mode")
    _seed(sid, [_item("sku1", "环保桌游", 25.0)])
    r = client.post(
        "/api/v1/cart/select_plan",
        params={"session_id": sid, "plan_id": "A", "mode": "overwrite"},
    )
    assert r.status_code == 400, r.text


def test_first_select_plan_on_empty_cart_does_not_emit_replacement_audit(client):
    """首次选方案 = 初始化购物车，不该伪装成「用户被替换掉了东西」。"""
    sid = _sid("cart-select-first")
    _seed(sid, cart=[], plan_a=[_item("sku1", "环保桌游", 25.0)])

    r = client.post(
        "/api/v1/cart/select_plan", params={"session_id": sid, "plan_id": "A"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replaced"] is False and body["removed_count"] == 0
    assert body["items_count"] == 1
    assert "cart_modified" not in _audit(client, sid)


# ---------------------------------------------------------------- 预算：报告不拦截

def test_quantity_change_over_budget_reports_guardrail_but_still_applies(client):
    sid = _sid("cart-guard")
    _seed(sid, [_item("sku1", "环保桌游", 25.0)], budget=50.0)

    r = client.patch(
        "/api/v1/cart/items/sku1", params={"session_id": sid}, json={"quantity": 3}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 超预算：事实要报出来
    assert body["guardrail"]["status"] == "fail"
    assert body["guardrail"]["ok"] is False
    assert body["guardrail"]["overage"] == 25.0, body["guardrail"]
    # 但不拦截：用户必须能「改到预算内再付」
    assert body["quantity"] == 3
    assert body["cart"]["total"] == 75.0


def test_guardrail_ok_when_within_budget(client):
    sid = _sid("cart-guard-ok")
    _seed(sid, [_item("sku1", "环保桌游", 25.0)], budget=100.0)
    r = client.patch(
        "/api/v1/cart/items/sku1", params={"session_id": sid}, json={"quantity": 2}
    )
    assert r.status_code == 200, r.text
    assert r.json()["guardrail"]["status"] == "ok"
    assert r.json()["guardrail"]["ok"] is True


def test_guardrail_reports_no_budget_instead_of_pretending_pass(client):
    """没有预算约束时说「没做复核」，而不是假装校验通过。"""
    sid = _sid("cart-guard-none")
    _seed(sid, [_item("sku1", "环保桌游", 25.0)])
    r = client.patch(
        "/api/v1/cart/items/sku1", params={"session_id": sid}, json={"quantity": 2}
    )
    assert r.status_code == 200, r.text
    assert r.json()["guardrail"]["status"] == "no_budget"



# ------------------------------------------------- 按件加购（POST /cart/items/{id}）

def test_add_single_item_from_plan_extends_cart(client):
    """「我只想买一件」：方案里有 3 件、车里只有 1 件 → ➕ 只把这一件加进去。"""
    sid = _sid("cart-add-one")
    cart = [_item("sku1", "环保桌游", 25.0)]
    plan = [
        _item("sku1", "环保桌游", 25.0),
        _item("sku2", "呢绒大衣", 120.0),
        _item("sku3", "R&B 黑胶", 30.0),
    ]
    _seed(sid, cart, plan_a=plan)

    r = client.post("/api/v1/cart/items/sku2", params={"session_id": sid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["item_id"] == "sku2"
    assert body["quantity"] == 1
    assert body["old_total"] == 25.0
    assert body["new_total"] == 145.0
    ids = [i["id"] for i in body["cart"]["items"]]
    assert ids == ["sku1", "sku2"], "只允许追加这一件，不许把整份方案灌进车"
    assert "cart_modified" in _audit(client, sid)


def test_add_item_twice_increments_quantity_not_duplicate_row(client):
    sid = _sid("cart-add-twice")
    _seed(sid, [], plan_a=[_item("sku2", "呢绒大衣", 120.0)])

    client.post("/api/v1/cart/items/sku2", params={"session_id": sid})
    r = client.post("/api/v1/cart/items/sku2", params={"session_id": sid})
    assert r.status_code == 200, r.text
    cart = client.get("/api/v1/cart", params={"session_id": sid}).json()
    assert len(cart["items"]) == 1, "同款应合并成一行，不是两行"
    assert cart["items"][0]["quantity"] == 2
    assert cart["total"] == 240.0


def test_add_item_outside_plan_is_404(client):
    """只认方案快照里的商品 —— 防止前端伪造任意商品/价格进车。"""
    sid = _sid("cart-add-404")
    _seed(sid, [], plan_a=[_item("sku2", "呢绒大衣", 120.0)])

    r = client.post("/api/v1/cart/items/forged_sku", params={"session_id": sid})
    assert r.status_code == 404
    assert "方案快照" in r.json()["detail"]
    assert client.get("/api/v1/cart", params={"session_id": sid}).json()["total"] == 0.0


def test_add_item_reports_budget_fact_without_blocking(client):
    sid = _sid("cart-add-budget")
    _seed(sid, [], plan_a=[_item("sku2", "呢绒大衣", 120.0)], budget=100.0)

    r = client.post("/api/v1/cart/items/sku2", params={"session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["guardrail"]["status"] == "fail"
    assert r.json()["guardrail"]["overage"] == 20.0

