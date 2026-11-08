"""P2-10 支付捕获节点加固测试：双字段隔离 + 幂等 + 显式失败态。

覆盖工单验收清单（真实 service + fake PayPal client，不触网）：
  1. real 两程续跑：create -> capture 共享同一 service 实例，capture_id 真实
     非 CAP-MOCK 开头，paypal_status=captured
  2. ID 隔离：shopping_intent_id（业务）与 paypal_intent_id（service uuid）不同源
  3. 负向：intent 未命中 -> capture_failed + evidence_caveats + capture_id=""
  4. 幂等：已 captured 不重复调 PayPal（绝不能二次扣款）
  5. 语义断言防火墙：real 分支收到 CAP-MOCK 开头 capture_id -> capture_failed
  6. mock 分支显式标记 paypal_mode=mock + paypal_source=mock
  7. decision_trace 与 capture 结果同源（order_id / capture_id / status 一致）
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ["LLM_MODE"] = "mock"
os.environ["PAYMENT_MODE"] = "mock"  # 通过 monkeypatch.nodes.PAYMENT_MODE 逐例切 real

from app.services.paypal.client import PayPalError
from app.services.paypal.service import PayPalPaymentService
from app.services.payment_agent import nodes


class FakePayPalClient:
    """不触网的 fake client：模拟 Orders v2 create / capture。"""

    def __init__(self, capture_id: str = "CAP-999") -> None:
        self.created: list[dict] = []
        self.create_kwargs: list[dict] = []
        self.captured: list[dict] = []
        self.fail_capture = False
        self.capture_id = capture_id  # 可注入 "CAP-MOCK-1" 验证防火墙

    async def create_order(self, **kwargs):
        self.create_kwargs.append(kwargs)
        order = {
            "id": "ORDER-123",
            "status": "CREATED",
            "links": [{"rel": "payer-action", "href": "https://sandbox.approve/ORDER-123"}],
        }
        self.created.append(order)
        return order

    async def capture_order(self, order_id: str):
        if self.fail_capture:
            raise PayPalError("ORDER_NOT_APPROVED", status_code=422)
        result = {
            "status": "COMPLETED",
            "purchase_units": [
                {"payments": {"captures": [{"id": self.capture_id, "status": "COMPLETED"}]}}
            ],
        }
        self.captured.append(result)
        return result

    @staticmethod
    def extract_approval_url(order: dict) -> str:
        for link in order.get("links", []):
            if link.get("rel") == "payer-action":
                return link["href"]
        return ""

    @staticmethod
    def extract_capture_id(order: dict) -> str:
        for pu in order.get("purchase_units", []):
            for pay in pu.get("payments", {}).get("captures", []):
                return pay["id"]
        return ""


def _create_state(**overrides) -> dict:
    state = {
        "user_id": "u-real",
        "raw_query": "帮我买一张爵士唱片",
        "shopping_intent_id": "biz-intent-001",
        "shopping_selected": {
            "name": "Sinatra 经典爵士黑胶",
            "price": 31.0,
            "currency": "USD",
            "match_tags": ["爵士", "黑胶"],
            "category": "music",
        },
        "shopping_rank_source": "local_ranker",
        "authority": {"verdict": "allow_auto", "reason": "高信任小额自动放行", "llm_reason": "老顾客习惯"},
    }
    state.update(overrides)
    return state


class TestCaptureHardening:
    @pytest.mark.asyncio
    async def test_real_two_step_create_then_capture(self, monkeypatch):
        """两程真实 service 链路：create 写入 paypal_intent_id，capture 只认它。"""
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
        svc = PayPalPaymentService(client=FakePayPalClient())
        state = _create_state(payment_service=svc)

        # 第一程：create —— 双字段隔离
        out_create = await nodes.paypal_create_node(state)
        assert out_create["paypal_intent_id"], "real 模式必须写入 service uuid"
        assert out_create["shopping_intent_id"] == "biz-intent-001", "业务 id 保持稳定"
        assert out_create["paypal_intent_id"] != out_create["shopping_intent_id"], "双字段必须不同源"
        assert out_create["paypal_order_id"] == "ORDER-123"
        assert out_create["paypal_mode"] == "real"

        # 关键断言：service 必须认 paypal_intent_id（不是业务 id）
        assert svc.get_intent(out_create["paypal_intent_id"]) is not None
        assert svc.get_intent("biz-intent-001") is None, "业务 id 不得被当成 service key"

        # 回跳前不能 capture；收到 PayPal return 才进入第二程。
        assert (await nodes.paypal_capture_node({**state, **out_create}))["paypal_status"] == "created"
        merged = {**state, **out_create, "payer_approved": True}

        # 第二程：capture —— 真实回执，禁止 mock id 混入
        out_cap = await nodes.paypal_capture_node(merged)
        assert out_cap["paypal_status"] == "captured", f"got {out_cap}"
        assert out_cap["capture_id"] == "CAP-999"
        assert not out_cap["capture_id"].startswith("CAP-MOCK"), "real 模式禁止 mock id"
        assert out_cap["paypal_mode"] == "real"

        # decision_trace 同源
        dt = out_cap["decision_trace"] or {}
        assert dt["capture_id"] == out_cap["capture_id"]
        assert dt["order_id"] == out_create["paypal_order_id"]
        assert dt["status"] == "captured"

    @pytest.mark.asyncio
    async def test_capture_idempotent_when_already_captured(self, monkeypatch):
        """幂等：state 已 captured -> 不重复调 PayPal（fake client 计数不变）。"""
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
        svc = PayPalPaymentService(client=FakePayPalClient())
        state = _create_state(
            payment_service=svc,
            paypal_intent_id="uuid-abc",
            paypal_order_id="ORDER-123",
            paypal_status="captured",
            capture_id="CAP-999",
            paypal_msg="支付成功（capture id=CAP-999）",
        )
        before = len(svc._client.captured)
        out = await nodes.paypal_capture_node(state)
        assert out["paypal_status"] == "captured"
        assert out["capture_id"] == "CAP-999"
        assert len(svc._client.captured) == before, "幂等命中后不得再次调用 PayPal capture"
    @pytest.mark.asyncio
    async def test_capture_missing_intent_fails_honestly(self, monkeypatch):
        """负向：service 内找不到 paypal_intent_id -> capture_failed + caveats。"""
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
        svc = PayPalPaymentService(client=FakePayPalClient())
        state = _create_state(
            payment_service=svc,
            paypal_intent_id="ghost-uuid-000",  # service 内不存在
            paypal_order_id="ORDER-GHOST",
            paypal_status="created",
            payer_approved=True,
        )
        out = await nodes.paypal_capture_node(state)
        assert out["paypal_status"] == "capture_failed", f"未命中必须显式失败，got {out}"
        assert out["capture_id"] == "", "失败态绝不返回 mock 回执"
        assert out["evidence_caveats"], "必须诚实标注原因"
        assert any("存在" in c for c in out["evidence_caveats"]), f"caveats 应写明找不到: {out['evidence_caveats']}"

    @pytest.mark.asyncio
    async def test_capture_missing_paypal_intent_id_field(self, monkeypatch):
        """负向：根本缺 paypal_intent_id -> capture_failed 且不触网。"""
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
        svc = PayPalPaymentService(client=FakePayPalClient())
        state = _create_state(payment_service=svc)  # 无 paypal_intent_id / paypal_order_id
        out = await nodes.paypal_capture_node(state)
        assert out["paypal_status"] == "capture_failed"
        assert out["capture_id"] == ""
        assert out["evidence_caveats"]
        assert len(svc._client.captured) == 0, "缺 id 时不得触网"

    @pytest.mark.asyncio
    async def test_payer_return_required_before_capture(self, monkeypatch):
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
        state = _create_state(paypal_status="created", paypal_intent_id="intent-1")
        out = await nodes.paypal_capture_node(state)
        assert out["paypal_status"] == "created"
        assert "等待买家" in out["paypal_msg"]

    @pytest.mark.asyncio
    async def test_payer_return_reuses_created_order(self, monkeypatch):
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
        svc = PayPalPaymentService(client=FakePayPalClient())
        state = _create_state(
            payment_service=svc,
            paypal_status="created",
            paypal_intent_id="intent-1",
            paypal_order_id="ORDER-123",
        )
        assert await nodes.paypal_create_node(state) == {}
        assert svc._client.created == []

    @pytest.mark.asyncio
    async def test_capture_mock_id_firewall_rejects(self, monkeypatch):
        """语义断言防火墙：service 异常返回 mock capture id -> 按失败处理。"""
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
        # 注入会在 capture 时返回 "CAP-MOCK-1" 的 client
        svc = PayPalPaymentService(client=FakePayPalClient(capture_id="CAP-MOCK-1"))
        intent = svc.propose_payment(user_id="u1", description="real 订单", amount="10.00")
        intent.paypal_order_id = "ORDER-123"
        intent.status = "created"

        state = _create_state(
            payment_service=svc,
            paypal_intent_id=intent.id,
            paypal_order_id="ORDER-123",
            paypal_status="created",
            payer_approved=True,
        )
        out = await nodes.paypal_capture_node(state)
        assert out["paypal_status"] == "capture_failed", f"防火墙必须拦截，got {out}"
        assert out["capture_id"] == ""
        assert out["evidence_caveats"]
        assert any("mock" in c.lower() for c in out["evidence_caveats"]), f"caveats 应写明 mock id 异常: {out['evidence_caveats']}"

    @pytest.mark.asyncio
    async def test_mock_mode_explicit_source_flag(self, monkeypatch):
        """mock 分支只由 PAYMENT_MODE=mock 触发，且带来源标记。"""
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "mock")
        out = await nodes.paypal_capture_node(_create_state())
        assert out["paypal_status"] == "captured"
        assert out["capture_id"] == "CAP-MOCK-1"
        assert out["paypal_mode"] == "mock"
        assert out["paypal_source"] == "mock", "mock 结果必须显式打标"

    @pytest.mark.asyncio
    async def test_capture_order_not_approved_is_explicit_failure(self, monkeypatch):
        """ORDER_NOT_APPROVED（买家未审批）-> capture_failed 而非假成功。"""
        monkeypatch.setattr(nodes, "PAYMENT_MODE", "real")
        svc = PayPalPaymentService(client=FakePayPalClient())
        svc._client.fail_capture = True  # type: ignore[attr-defined]
        intent = svc.propose_payment(user_id="u1", description="x", amount="5.00")
        intent2, _ = await svc.create_payment_order(intent.id)
        state = _create_state(
            payment_service=svc,
            paypal_intent_id=intent2.id,
            paypal_order_id=intent2.paypal_order_id,
            paypal_status="created",
            payer_approved=True,
        )
        out = await nodes.paypal_capture_node(state)
        assert out["paypal_status"] == "capture_failed"
        assert out["capture_id"] == ""
        assert out["evidence_caveats"]
        assert any("审批" in c for c in out["evidence_caveats"]), "应写明买家尚未审批"