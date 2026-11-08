"""支付 Agent HTTP API 冒烟测试 —— 验证主图（含支付子图）真正接进 HTTP。

覆盖 RootUser P0-1 的核心验收：
  1. POST /api/v1/payment/agent 第一程停在审批闸门（needs_approval=true）
  2. POST /api/v1/payment/approve 第二程批准后跑完支付（订单创建 + 捕获 + 解释）
  3. 否决路径终止
  4. 响应里带可解释性面板证据分组（P2-7 数据源）

注意：TestClient 与 app 共享同一进程 -> payment._get_graph() 单例 + MemorySaver
checkpoint 跨请求存活，这是审批恢复能在 HTTP 层工作的前提。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ["LLM_MODE"] = "mock"
os.environ["PAYMENT_MODE"] = "mock"
os.environ["GRAPH_ENV"] = "dev"

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    """每个 module 一个 TestClient（共享进程 -> 共享 graph 单例 + checkpoint）。"""
    with TestClient(app) as c:
        yield c


def test_payment_agent_need_approval_then_approve(client):
    """第一程停在审批 -> 第二程批准 -> 跑完支付链路。"""
    session = "http-approve-flow"
    r1 = client.post("/api/v1/payment/agent", json={
        "message": "帮我买一张周五晚上听的爵士唱片",
        "session_id": session,
        "user_id": "http-user-ok",
        "monthly_spent": 20.0,
    })
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["needs_approval"] is True, f"新用户应停在审批，got {body1.get('status')}"
    assert body1["authority"]["verdict"] == "need_approval"
    assert body1["authority"]["reason"]
    assert body1["authority"]["trust_score"] is not None
    assert body1["session_id"] == session

    # 第二程批准
    r2 = client.post("/api/v1/payment/approve", json={
        "session_id": session,
        "user_id": "http-user-ok",
        "decision": "approve",
        "comment": "放心买",
    })
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["status"] == "approved" or body2["status"] == "captured"
    assert body2["payment"]["order_id"] == "ORDER-MOCK-123", f"mock 应创建订单，got {body2['payment']}"
    assert body2["payment"]["capture_id"] == "CAP-MOCK-1"
    assert body2["explain"]["text"], "事后解释必须存在"
    assert body2["explain"]["trust_updated"], "信任分必须回写"
    assert float(body2["explain"]["trust_updated"]["new_score"]) > float(
        body2["explain"]["trust_updated"]["old_score"]
    )
    assert body2["authority"]["verdict"] == "need_approval"  # 授权判定保留


def test_payment_agent_reject_terminates(client):
    """否决路径：不创建订单。"""
    session = "http-reject-flow"
    r1 = client.post("/api/v1/payment/agent", json={
        "message": "帮我买一张周五晚上听的爵士唱片",
        "session_id": session,
        "user_id": "http-user-rej",
        "monthly_spent": 20.0,
    })
    assert r1.status_code == 200, r1.text
    assert r1.json()["needs_approval"] is True

    r2 = client.post("/api/v1/payment/approve", json={
        "session_id": session,
        "user_id": "http-user-rej",
        "decision": "reject",
        "comment": "超预算，不买",
    })
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["status"] == "cancelled"
    assert body2["payment"]["order_id"] in ("", None), "否决后不得创建订单"


def test_payment_response_contains_evidence_groups(client):
    """响应必须带可解释性面板证据分组（P2-7 前端消费数据源）。"""
    session = "http-evidence-flow"
    r1 = client.post("/api/v1/payment/agent", json={
        "message": "帮我买一张周五晚上听的爵士唱片",
        "session_id": session,
        "user_id": "http-user-ev",
        "monthly_spent": 20.0,
    })
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert isinstance(body["evidence"], list)
    titles = [g.get("title") for g in body["evidence"]]
    assert "为什么这样授权" in titles, f"证据分组应含授权依据，got {titles}"
    # 待审批阶段还不到支付凭证/信任分变化（尚未捕获/回写）
    assert all(isinstance(g.get("rows"), list) for g in body["evidence"])


def test_payment_agent_non_shopping_message_not_hard_fail(client):
    """非支付消息走主图不硬失败（降级/澄清路径）。"""
    r = client.post("/api/v1/payment/agent", json={
        "message": "你好，介绍一下你自己",
        "session_id": "http-chat-flow",
        "user_id": "http-user-chat",
        "monthly_spent": 0.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"], "非支付消息也要有回复"
    assert body["needs_approval"] is False


def test_trust_score_persists_across_sessions(client):
    """P0-3：信任分跨会话恢复——新 session 同 user 继承上次 A/B 级累计分数。

    会话1 批准一笔后信任分连续累积：
      0.30 ->(A 级用户批准 +0.12)-> 0.42 ->(B 级订单捕获 +0.08)-> 0.50
    explain_node 的 old_score 是 A 级后的 0.42（A 级已写回 state），最终落盘 0.50。
    会话2 新 thread 不应重回 0.3，而应直接以 0.50 进入 authority_check。
    """
    # 会话 1：批准一笔
    r1 = client.post("/api/v1/payment/agent", json={
        "message": "帮我买一张周五晚上听的爵士唱片",
        "session_id": "http-trust-1",
        "user_id": "http-user-trust",
        "monthly_spent": 20.0,
    })
    assert r1.status_code == 200, r1.text
    assert r1.json()["needs_approval"] is True

    r2 = client.post("/api/v1/payment/approve", json={
        "session_id": "http-trust-1",
        "user_id": "http-user-trust",
        "decision": "approve",
        "comment": "OK",
    })
    assert r2.status_code == 200, r2.text
    tu = r2.json()["explain"]["trust_updated"]
    assert tu["old_score"] == 0.42, f"A 级批准后应为 0.42, got {tu['old_score']}"
    assert tu["new_score"] == 0.5, f"A+B 级累计应为 0.5, got {tu['new_score']}"

    # 会话 2：全新 session，同一 user_id -> 信任分应跨会话恢复为 0.5
    r3 = client.post("/api/v1/payment/agent", json={
        "message": "帮我买一张黑胶唱片",
        "session_id": "http-trust-2",
        "user_id": "http-user-trust",
        "monthly_spent": 0.0,
    })
    assert r3.status_code == 200, r3.text
    body3 = r3.json()
    assert body3["authority"]["trust_score"] == 0.5, (
        f"信任分应跨会话恢复为 0.5, got {body3['authority']['trust_score']}"
    )

    # 第三程：0.5 仍低于自动放行门槛 0.6 -> 依旧停在审批（渐进式授权：信任逐步累积）
    assert body3["needs_approval"] is True


@pytest.mark.parametrize("high_trust", [False, True], ids=["manual-approval", "auto-approval"])
def test_real_paypal_return_uses_shared_callback_before_capture(monkeypatch, high_trust):
    """无论是否免应用审批，建单后都必须等待买家回跳才能捕获。"""
    from app.api import payment as payment_api
    from app.services.payment_agent import nodes
    from app.services.paypal import service as paypal_service
    from app.services.paypal.service import PayPalPaymentService
    from tests.test_payment_capture_hardening import FakePayPalClient

    fake = FakePayPalClient()
    service = PayPalPaymentService(client=fake)
    session = f"http-real-payer-return-{high_trust}"
    monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
    monkeypatch.setattr(paypal_service, "get_payment_service", lambda: service)
    if high_trust:
        from dataclasses import replace
        from app.services.paypal.authz import TrustProfile

        trusted = replace(TrustProfile(), trust_score=0.9, auto_threshold=100.0)
        monkeypatch.setattr(payment_api, "_load_trust_profile", lambda _user_id: trusted)
    from app.services.graph.graph import build_agent_graph

    graph = build_agent_graph(interrupt_before_paypal_capture=True)
    assert "payment_paypal_capture" in graph.interrupt_before_nodes
    monkeypatch.setattr(payment_api, "_graph_instance", graph)
    monkeypatch.setattr(payment_api, "_get_graph", lambda: graph)
    from fastapi.testclient import TestClient
    client = TestClient(app)

    r1 = client.post("/api/v1/payment/agent", json={
        "message": "帮我买一张爵士唱片",
        "session_id": session,
        "user_id": "http-user-real-return",
    })
    assert r1.status_code == 200, r1.text
    assert r1.json()["needs_approval"] is not high_trust
    if high_trust:
        assert r1.json()["authority"]["verdict"] == "allow_auto"
        r2 = r1  # 高信任仅跳过应用审批，PayPal 买家审批仍不可省略。
    else:
        r2 = client.post("/api/v1/payment/approve", json={
            "session_id": session,
            "user_id": "http-user-real-return",
            "decision": "approve",
        })
    body2 = r2.json()
    assert r2.status_code == 200, r2.text
    assert body2["payment"]["status"] == "created"
    assert body2["payment"]["capture_id"] == ""
    assert len(fake.created) == 1 and fake.captured == []
    # 与团购使用同一后端回跳入口；经前端代理转发。
    assert "/api/v1/paypal/callback?flow=payment" in fake.create_kwargs[0]["return_url"]

    bad = client.get(
        "/api/v1/paypal/callback",
        params={"flow": "payment", "session_id": session, "token": "WRONG"},
    )
    assert bad.status_code == 409 and fake.captured == []
    returned = client.get(
        "/api/v1/paypal/callback",
        params={
            "flow": "payment",
            "session_id": session,
            "token": "ORDER-123",
            "PayerID": "PAYER-1",
        },
    )
    body3 = returned.json()
    assert returned.status_code == 200, returned.text
    assert body3["payment"]["status"] == "captured"
    assert body3["payment"]["capture_id"] == "CAP-999"
    assert body3["explain"]["trust_updated"]["evidence_grade"] == "B"
    assert len(fake.created) == 1 and len(fake.captured) == 1
    assert session not in payment_api._PENDING_PAYMENTS
