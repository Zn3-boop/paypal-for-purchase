"""GET /plans 快照语义回归 —— 2026-11-04 事故的疫苗。

事故链（全部真实发生，探针级 vs 点击级三种同款事故之一）：
  - PlansTab 每 5s 轮询 GET /plans（apps/web/src/components/tabs/PlansTab.tsx:53-60）；
  - 旧实现每次 GET 都重新生成方案并写一条 plan_generated 审计（plans.py）；
  - 结果一：加完 350 预算 5s 内方案 B 被静默改写 393→350，购物车随之合规，
    「购物车 393 > 最新预算 350 → 支付前 D2 主门 400」这条治理主线在 UI 里永远打不出来；
  - 结果二：审计时间轴被 5s 一条 plan_generated 淹没，13 事件表无法在 UI 录制下达成。

新契约（快照语义，请勿"优化"回缓存失效语义）：
  方案在会话内首次 GET 时生成一次并把完整响应落盘 session.plans_snapshot；
  此后所有 GET 只原样回放：不重算、不写审计。约束收紧不静默改写用户正在看的方案，
  守卫在支付时刻用最新约束把关。只有 /session/reset 能让快照失效。
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


@pytest.fixture(scope="module")
def client():
    """每个 module 一个 TestClient（与 test_payment_http_api.py 同一约定）。"""
    with TestClient(app) as c:
        yield c


def _sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _plans(client, sid: str) -> dict:
    r = client.get("/api/v1/plans", params={"session_id": sid})
    assert r.status_code == 200, r.text
    return r.json()


def _plan_total(payload: dict, plan_id: str) -> float:
    return next(p["total"] for p in payload["plans"] if p["plan_id"] == plan_id)


def _plan_generated_count(client, sid: str) -> int:
    r = client.get("/api/v1/audit/logs", params={"session_id": sid, "limit": 500})
    assert r.status_code == 200, r.text
    body = r.json()
    return sum(1 for e in body["logs"] if e["event"] == "plan_generated")


def _reset(client, sid: str, hard: bool) -> None:
    r = client.post(
        "/api/v1/session/reset",
        json={"session_id": sid, "user_id": "snapshot-tester", "hard_reset": hard},
    )
    assert r.status_code == 200, r.text


def test_repeated_get_plans_replays_snapshot_and_logs_once(client):
    """反复开关方案 Tab（5s 轮询）只能有一次 plan_generated，且载荷原样一致。"""
    sid = _sid("snapshot-once")
    _reset(client, sid, hard=True)

    first = _plans(client, sid)
    assert first["feasible"] is True and len(first["plans"]) == 2

    # 轮询 + 开关 Tab = 任意次 GET，都不得产生新事件、不得改写载荷
    for _ in range(3):
        replay = _plans(client, sid)
        assert replay == first, "GET /plans 必须原样回放快照（载荷一致）"

    assert _plan_generated_count(client, sid) == 1, "轮询不得重复记账 plan_generated"


def test_plans_snapshot_keeps_budget_gate_alive(client):
    """主线缩影：600 生成 → 加 350 → 回放仍 B=393 → 选 B → 400 budget_violated。

    这是整场事故的疫苗测试：只要它存在，这类问题永远不会再从探针层漏到点击层。
    """
    sid = _sid("snapshot-gate")
    _reset(client, sid, hard=True)

    r = client.post(
        "/api/v1/constraints",
        params={"session_id": sid},
        json={"type": "budget", "content": "总预算 600 美元", "source": "成员A",
              "hard": True, "numeric_value": 600},
    )
    assert r.status_code == 200, r.text

    payload = _plans(client, sid)
    plan_b_snapshot = next(p for p in payload["plans"] if p["plan_id"] == "B")
    assert _plan_total(payload, "B") == 393.0, "计划基线必须是 B=393.0（剧本锚点）"
    assert _plan_generated_count(client, sid) == 1

    # 预算收紧到 350：约束进入面板，但方案不动（治理叙事核心）
    r = client.post(
        "/api/v1/constraints",
        params={"session_id": sid},
        json={"type": "budget", "content": "预算压到 350", "source": "成员B",
              "hard": True, "numeric_value": 350},
    )
    assert r.status_code == 200, r.text

    replay = _plans(client, sid)
    assert _plan_total(replay, "B") == 393.0, "约束收紧后方案被静默改写（快照语义失效）"
    assert next(p for p in replay["plans"] if p["plan_id"] == "B") == plan_b_snapshot, \
        "约束收紧后载荷不得有任何变化"
    assert _plan_generated_count(client, sid) == 1, "约束变更不得触发第二次方案生成"

    # 选 B → 购物车 393.0 > 最新预算 350.0 → D2 主门 400，overage 43.0
    r = client.post(
        "/api/v1/cart/select_plan",
        params={"session_id": sid, "plan_id": "B"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 393.0

    r = client.post(
        "/api/v1/paypal/create_order",
        json={
            "amount": 393.0,
            "session_id": sid,
            "user_email": "demo@example.com",
            "item_name": "ECV Group Purchase",
        },
    )
    assert r.status_code == 400, f"预算守卫必须拦截：{r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert detail["status"] == "budget_violated", detail
    assert abs(detail["overage"] - 43.0) < 1e-6, detail
    assert abs(detail["budget_limit"] - 350.0) < 1e-6, detail
    assert abs(detail["cart_total"] - 393.0) < 1e-6, detail