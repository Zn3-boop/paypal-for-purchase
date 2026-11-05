# -*- coding: utf-8 -*-
from __future__ import annotations

"""Golden Path regression test (Batch 2 baseline).

Expected event sequence (user-approved golden path):
  constraint_changed -> plan_generated -> cart_modified -> review_confirmed
  -> order_status_changed (draft->confirmed)
  -> order_status_changed (confirmed->paying)
  -> payment_completed
  -> order_status_changed (paying->paid)

Usage: cd apps/server && python scripts/golden_path_verify.py

Normal path: 8 events in strict order, payment_completed before paying->paid.
Abnormal path: simulate amount mismatch (D2 sub-gate) -> payment_amount_mismatch event + cancelled order.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))
os.chdir(SERVER_ROOT)
# When run from repo-root scripts/ (actual location), SERVER_ROOT resolves to
# the repo root; navigate into apps/server where the `app` package lives.
if not (SERVER_ROOT / "app").exists():
    SERVER_ROOT = SERVER_ROOT / "apps" / "server"
    sys.path.insert(0, str(SERVER_ROOT))
    os.chdir(SERVER_ROOT)

from fastapi.testclient import TestClient
from app.services.group_purchase.order_lifecycle import list_order_records, OrderStatus
from app.services.group_purchase.session_state import get_or_create_session


class FakePayPalGateway:
    """In-memory PayPal gateway stub for local dev (no real credentials)."""

    def __init__(self) -> None:
        self.counter = 0
        self._force_fail_capture = False

    def create_order(self, amount: float, item_name: str = "ECV Group Purchase Order",
                     return_url: str | None = None, cancel_url: str | None = None,
                     reference_id: str = "group-purchase-ref", currency: str = "USD"):
        self.counter += 1
        token = f"FAKE-ORDER-{self.counter:06d}"
        return {
            "order_id": token,
            "approval_url": f"https://sandbox.paypal.com/fake-approve/{token}",
            "status": "CREATED",
        }

    def get_order(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "APPROVED"}

    def capture_order(self, order_id: str) -> dict:
        if self._force_fail_capture:
            # Abnormal path: force amount mismatch to trigger sub-gate failure
            return {
                "order_id": order_id,
                "status": "COMPLETED",
                "amount": 999.99,
                "currency": "USD",
                "final_capture": True,
                "seller_protection": "ELIGIBLE",
                "payer_email": "demo-buyer@example.com",
            }
        return {
            "order_id": order_id,
            "status": "COMPLETED",
            "capture_id": f"CAP-FAKE-{order_id}",
            "amount": 24.5,
            "currency": "USD",
            "final_capture": True,
            "seller_protection": "ELIGIBLE",
            "payer_email": "demo-buyer@example.com",
        }


FAKE_GATEWAY = os.getenv("FAKE_GATEWAY", "0") == "1"


def _plan_a_snapshot(payload: dict) -> list[dict]:
    """从 /plans 响应里取出 Plan A 的商品行，用作内容级比对。"""
    plans = payload.get("plans") or []
    plan_a = next((p for p in plans if p.get("plan_id") == "A"), None)
    if not plan_a:
        return []
    return [
        {"id": item.get("id"), "name": item.get("name"), "price": item.get("price")}
        for item in (plan_a.get("items") or [])
    ]


def run_a5_delete_boundary(client) -> dict:
    """A5 边界证据：约束是规划输入 —— 删掉禁忌约束后重算，方案内容变化，再恢复。

    快照语义（2026-11-04 起）：约束变更**不**触发重算，GET /plans 只回放首次生成的快照；
    只有 /session/reset 能让快照失效。所以这里的"重生成"通过 reset 显式触发，
    这恰好同时验证了两件事：
      1. 改约束不会静默改写正在看的方案（快照不变）；
      2. 约束确实参与方案装配（reset 后重算，内容按约束变化/恢复）。
    删除不是装饰性动作：约束参与前置过滤与方案装配。这里用方案内容（商品 id 集合）
    做断言，而不只看 200。为了不依赖商品池里恰好有“塑料”条目，禁忌词直接取自
    当前 Plan A 的一个商品名，确保该约束一定命中并在删除后恢复。
    """
    session_id = f"golden-a5-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    client.post(
        "/api/v1/session/reset",
        json={"session_id": session_id, "user_id": "demo-user", "hard_reset": True},
    )
    r = client.post(
        f"/api/v1/constraints?session_id={session_id}",
        json={"type": "budget", "content": "Total budget not exceed $600",
              "source": "member-A", "hard": True, "numeric_value": 600},
    )
    assert r.status_code == 200, f"a5 budget constraint: {r.status_code} {r.text}"

    base_payload = client.get(f"/api/v1/plans?session_id={session_id}").json()
    baseline = _plan_a_snapshot(base_payload)
    assert len(baseline) >= 2, f"a5 baseline plan too small: {baseline}"
    target = baseline[-1]

    r = client.post(
        f"/api/v1/constraints?session_id={session_id}",
        json={"type": "taboo", "content": f"不要{target['name']}",
              "source": "member-B", "hard": True},
    )
    assert r.status_code == 200, f"a5 taboo constraint: {r.status_code} {r.text}"
    taboo_id = r.json()["constraint_id"]

    # 快照语义断言：新约束只进约束面板，不改写方案（轮询安全）
    replay_payload = client.get(f"/api/v1/plans?session_id={session_id}").json()
    replay = _plan_a_snapshot(replay_payload)
    assert {row["id"] for row in replay} == {row["id"] for row in baseline}, (
        "a5: plan must stay a snapshot after a constraint change (no silent replan)"
    )

    # 显式失效（软 reset：保留全部约束）→ 重算 → 禁忌生效
    r = client.post(
        f"/api/v1/session/reset",
        json={"session_id": session_id, "user_id": "demo-user", "hard_reset": False},
    )
    assert r.status_code == 200, f"a5 soft reset: {r.status_code} {r.text}"

    filtered_payload = client.get(f"/api/v1/plans?session_id={session_id}").json()
    filtered = _plan_a_snapshot(filtered_payload)
    filtered_ids = {row["id"] for row in filtered}
    base_ids = {row["id"] for row in baseline}
    assert filtered, "a5: plan became empty after taboo (unexpected)"
    assert target["id"] not in filtered_ids, "a5: taboo did not remove the targeted item"
    assert filtered_ids != base_ids, "a5: plan content unchanged after taboo"

    r = client.delete(f"/api/v1/constraints/{taboo_id}?session_id={session_id}")
    assert r.status_code == 200, f"a5 delete: {r.status_code} {r.text}"

    r = client.post(
        f"/api/v1/session/reset",
        json={"session_id": session_id, "user_id": "demo-user", "hard_reset": False},
    )
    assert r.status_code == 200, f"a5 second soft reset: {r.status_code} {r.text}"

    restored_payload = client.get(f"/api/v1/plans?session_id={session_id}").json()
    restored = _plan_a_snapshot(restored_payload)
    restored_ids = {row["id"] for row in restored}
    assert restored_ids == base_ids, (
        f"a5: plan did not restore after deleting constraint: {restored_ids} != {base_ids}"
    )
    assert target["id"] in restored_ids, "a5: targeted item missing after reopen"

    listed = client.get(f"/api/v1/constraints?session_id={session_id}").json()
    assert taboo_id not in {c["id"] for c in listed.get("constraints", [])}, (
        "a5: deleted constraint still visible on dashboard"
    )

    result = {
        "session_id": session_id,
        "deleted_constraint_id": taboo_id,
        "target_item": target,
        "baseline_ids": sorted(base_ids),
        "filtered_ids": sorted(filtered_ids),
        "restored_ids": sorted(restored_ids),
        "snapshot_stable_across_constraint_change": True,
        "content_changed_on_replan": True,
        "restored_on_reopen": True,
        "ok": True,
    }
    print("A5 boundary: delete constraint → replan(via reset) → content changed → reopen restored")
    print(f"  baseline : {sorted(base_ids)}")
    print(f"  filtered : {sorted(filtered_ids)}")
    print(f"  restored : {sorted(restored_ids)}")
    print(f"  PASS: {result['ok']}")
    return result


def build_app():
    from fastapi import FastAPI
    app = FastAPI(title="golden-path-test")
    from app.api import orders, paypal_callback, constraints, plans, cart, session as session_api
    app.include_router(paypal_callback.router, prefix="/api/v1")
    app.include_router(orders.router, prefix="/api/v1")
    app.include_router(constraints.router, prefix="/api/v1")
    app.include_router(plans.router, prefix="/api/v1")
    app.include_router(cart.router, prefix="/api/v1")
    app.include_router(session_api.router, prefix="/api/v1")
    return app


def main() -> int:
    app = build_app()
    client = TestClient(app)

    if FAKE_GATEWAY:
        import app.services.paypal_gateway as gw_module
        import app.api.paypal_callback as cb_module
        fake = FakePayPalGateway()
        gw_module.get_paypal_gateway = lambda: fake
        cb_module.get_paypal_gateway = lambda: fake

    session_id = f"golden-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # Reset / create session so downstream lookups succeed
    r = client.post(
        f"/api/v1/session/reset",
        json={"session_id": session_id, "user_id": "demo-user", "hard_reset": True},
    )
    assert r.status_code == 200, f"session reset: {r.status_code} {r.text}"

    # ========== Normal path (golden path) ==========
    print("[1/6] Setting constraint (budget hard cap 600)...")
    r = client.post(
        f"/api/v1/constraints?session_id={session_id}",
        json={"type": "budget", "content": "Total budget not exceed $600",
              "source": "member-A", "hard": True, "numeric_value": 600}
    )
    assert r.status_code == 200, f"constraints: {r.status_code} {r.text}"

    print("[2/6] Generating dual plans...")
    r = client.get(f"/api/v1/plans?session_id={session_id}")
    assert r.status_code == 200, f"plans: {r.status_code} {r.text}"
    plans = r.json()
    assert plans.get("plans"), "plans is empty"

    print("[3/6] Selecting plan A to cart...")
    r = client.post(
        f"/api/v1/cart/select_plan?session_id={session_id}&plan_id=A"
    )
    assert r.status_code == 200, f"select_plan: {r.status_code} {r.text}"

    print("[4/6] Cart modification (natural-language)...")
    r = client.post(
        f"/api/v1/cart/modify?session_id={session_id}",
        json={"instruction": "Delete first item"}
    )
    if r.status_code != 200:
        print(f"⚠️ LLM modification failed: {r.status_code} {r.text}")
    cart_after = client.get(f"/api/v1/cart?session_id={session_id}").json()
    total = cart_after["total"]

    print("[5/6] Creating PayPal order (triggers review_confirmed + confirmed->paying)...")
    r = client.post(
        "/api/v1/paypal/create_order",
        json={"amount": total, "session_id": session_id,
              "user_email": "demo-user@example.com", "item_name": "ECV Group Purchase"}
    )
    assert r.status_code == 200, f"create_order: {r.status_code} {r.text}"
    paypal_token = r.json()["order_id"]

    print("[6/6] PayPal callback capture (triggers payment_completed + paying->paid)...")
    r = client.get(
        f"/api/v1/paypal/callback?token={paypal_token}&PayerID=FAKE-BUYER"
    )
    assert r.status_code == 200, f"callback: {r.status_code} {r.text}"
    cb = r.json()
    assert cb.get("success") is True, f"callback did not succeed: {cb}"

    # Verify audit timeline sequence
    from app.services.group_purchase.audit_log import get_audit_logger
    logs = get_audit_logger().get_session_timeline(session_id)

    print("=" * 60)
    print("Golden path timeline (chronological)")
    print("=" * 60)
    for idx, entry in enumerate(logs, start=1):
        extra = ""
        if entry.event == "order_status_changed":
            d = entry.data or {}
            extra = f"  {d.get('from_status')}→{d.get('to_status')} (order={d.get('order_id')})"
        elif entry.event == "cart_modified":
            d = entry.data or {}
            extra = f"  type={d.get('modification_type')} ids={d.get('item_ids')}"
        print(f"[{idx:02d}] {entry.event}{extra}")

    expected = [
        "constraint_changed",
        "plan_generated",
        "cart_modified",
        "review_confirmed",
        "order_status_changed",
        "order_status_changed",
        "payment_completed",
        "order_status_changed",
    ]
    actual = [
        e.event for e in logs
        if e.event in {"constraint_changed", "plan_generated", "cart_modified",
                       "review_confirmed", "payment_completed", "order_status_changed"}
    ][::-1]  # timeline returns newest-first

    matched = len(actual) == len(expected) and all(a == b for a, b in zip(actual, expected))
    print("-" * 60)
    print("Expected:", " → ".join(expected))
    print("Actual  :", " → ".join(actual))
    print("Match   :", "PASS" if matched else "FAIL")

    # Verify order lifecycle record
    records = list_order_records(session_id=session_id)
    assert len(records) == 1, f"expected 1 order, got {len(records)}"
    order = records[0]
    assert order.status == OrderStatus.PAID, f"order should be PAID, got {order.status}"
    transitions = [t["to"] for t in order.transitions]
    assert transitions == ["confirmed", "paying", "paid"], f"unexpected transitions: {transitions}"

    # Archive snapshot for diff regression
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    docs_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = docs_dir / f"golden_path_timeline_{stamp}.json"
    snapshot = {
        "session_id": session_id,
        "order": order.to_dict(),
        "events": [e.to_dict() for e in logs],
        "expected_sequence": expected,
        "matched": matched,
        "created_at": datetime.now().isoformat(),
    }
    archive.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nArchived: {archive}")
    print(f"Order: {order.order_id} status={order.status.value} total={order.total}")

    # ========== Abnormal path: simulate D2 sub-gate failure ==========
    print("\n" + "=" * 60)
    print("Abnormal path test: simulate PayPal amount tampering (trigger payment_amount_mismatch)")
    print("=" * 60)

    if FAKE_GATEWAY:
        session_id_mal = f"golden-mal-{session_id}"
        client.post(
            f"/api/v1/session/reset",
            json={"session_id": session_id_mal, "user_id": "demo-user", "hard_reset": True},
        )
        client.post(
            f"/api/v1/session/reset",
            json={"session_id": session_id_mal, "user_id": "demo-user", "hard_reset": True},
        )
        client.post(
            f"/api/v1/constraints?session_id={session_id_mal}",
            json={"type": "budget", "content": "Budget 600", "source": "A", "hard": True, "numeric_value": 600}
        )
        client.get(f"/api/v1/plans?session_id={session_id_mal}")
        client.post(f"/api/v1/cart/select_plan?session_id={session_id_mal}&plan_id=A")
        client.post(f"/api/v1/cart/modify?session_id={session_id_mal}", json={"instruction": "Delete first"})
        cart_mal = client.get(f"/api/v1/cart?session_id={session_id_mal}").json()
        r = client.post(
            "/api/v1/paypal/create_order",
            json={"amount": cart_mal["total"], "session_id": session_id_mal,
                  "user_email": "demo@example.com", "item_name": "malicious-tamper"}
        )
        token_mal = r.json()["order_id"]

        from app.services.paypal_gateway import get_paypal_gateway
        fake_gw = get_paypal_gateway()
        fake_gw._force_fail_capture = True

        r = client.get(f"/api/v1/paypal/callback?token={token_mal}&PayerID=FAKE-BUYER")
        assert r.status_code == 200, f"callback(mal): {r.status_code}"
        assert r.json().get("success") is False, "sub-gate interception failed"
        assert r.json().get("reason") == "amount_mismatch", "reason mismatch"

        logs_mal = get_audit_logger().get_session_timeline(session_id_mal)
        has_mismatch = any(e.event == "payment_amount_mismatch" for e in logs_mal)
        print(f"payment_amount_mismatch event logged: {'PASS' if has_mismatch else 'FAIL'}")
        for e in logs_mal:
            print(f"   [{e.event}] data={e.data}")

        rec_mal = list_order_records(session_id=session_id_mal)[0]
        assert rec_mal.status == OrderStatus.CANCELLED, \
            f"abnormal-order should be CANCELLED, got {rec_mal.status}"
    else:
        print("⚠️ Abnormal path skipped (real gateway mode, cannot simulate tampering)")
    # ========== A5 delete boundary: delete constraint -> replan -> content changes -> restore ==========
    print("[7/7] A5 delete-boundary: delete constraint -> replan -> content changes -> restore...")
    a5_result = run_a5_delete_boundary(client)
    assert a5_result["ok"], f"A5 boundary failed: {a5_result}"


    print("\n" + ("=" * 60))
    print("golden path PASS" if matched else "golden path FAIL")
    return 0 if matched else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)