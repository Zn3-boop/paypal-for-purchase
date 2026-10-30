"""PayPal REST API 客户端（Orders v2 + Payouts v1）。

设计要点：
  - 懒创建 httpx.AsyncClient（共享懒加载客户端模式）
  - OAuth token 带简单进程内缓存（过期前 30 秒自动刷新）
  - create_order / capture_order / get_order / create_payout 四个核心方法
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from app.services.paypal.config import PayPalConfig

logger = logging.getLogger(__name__)


class PayPalError(RuntimeError):
    """PayPal API 业务/鉴权错误。"""

    def __init__(self, message: str, *, status_code: int = 0, debug_id: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.debug_id = debug_id


class PayPalClient:
    def __init__(self, config: PayPalConfig | None = None) -> None:
        self._config = config or PayPalConfig.from_env()
        self._http: httpx.AsyncClient | None = None
        self._token: str = ""
        self._token_expires_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def config(self) -> PayPalConfig:
        return self._config

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self._config.api_base,
                timeout=httpx.Timeout(self._config.timeout_seconds, connect=10.0),
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    async def get_access_token(self, *, force: bool = False) -> str:
        """返回缓存的 access token；过期前 30s 或 force=True 时刷新。"""
        now = time.time()
        if not force and self._token and self._token_expires_at - now > 30:
            return self._token
        basic = base64.b64encode(
            f"{self._config.client_id}:{self._config.secret}".encode()
        ).decode()
        http = await self._get_http()
        resp = await http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code >= 400:
            raise PayPalError(
                f"OAuth token 获取失败: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = now + float(data.get("expires_in", 3600))
        return self._token

    async def _authed_request(
        self, method: str, path: str, *, json: Any = None, params: dict | None = None
    ) -> httpx.Response:
        token = await self.get_access_token()
        http = await self._get_http()
        return await http.request(
            method,
            path,
            json=json,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    async def create_order(
        self,
        *,
        amount: str,
        currency: str = "USD",
        description: str = "",
        reference_id: str = "",
        brand_name: str = "Channel3 Agent",
        return_url: str = "https://example.com/return",
        cancel_url: str = "https://example.com/cancel",
    ) -> dict[str, Any]:
        """创建 CAPTURE 意图订单，返回含 approval URL 的 order dict。"""
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": reference_id or "channel3-paypal",
                    "description": description or "Channel3 Agentic Shopping",
                    "amount": {"currency_code": currency, "value": amount},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                        "brand_name": brand_name,
                        "shipping_preference": "NO_SHIPPING",
                        "user_action": "PAY_NOW",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }
        resp = await self._authed_request("POST", "/v2/checkout/orders", json=payload)
        if resp.status_code >= 400:
            raise PayPalError(
                f"create order 失败: {resp.text[:400]}",
                status_code=resp.status_code,
            )
        return resp.json()

    async def get_order(self, order_id: str) -> dict[str, Any]:
        resp = await self._authed_request("GET", f"/v2/checkout/orders/{order_id}")
        if resp.status_code >= 400:
            raise PayPalError(
                f"get order {order_id} 失败: {resp.text[:400]}",
                status_code=resp.status_code,
            )
        return resp.json()

    async def capture_order(self, order_id: str) -> dict[str, Any]:
        """对已审批订单执行 capture。未审批时 PayPal 返回 ORDER_NOT_APPROVED (422)。"""
        resp = await self._authed_request(
            "POST", f"/v2/checkout/orders/{order_id}/capture"
        )
        if resp.status_code >= 400:
            raise PayPalError(
                f"capture 失败: {resp.text[:400]}",
                status_code=resp.status_code,
            )
        return resp.json()

    @staticmethod
    def extract_approval_url(order: dict[str, Any]) -> str:
        for link in order.get("links", []):
            if link.get("rel") == "payer-action":
                return link["href"]
        return ""

    @staticmethod
    def extract_capture_id(order: dict[str, Any]) -> str:
        for pu in order.get("purchase_units", []):
            for pay in pu.get("payments", {}).get("captures", []):
                return pay["id"]
        return ""
# ----------------------------------------------------------
    # Payouts v1（彩蛋：代理间付款；字段是 receiver 不是 recipient）
    # ----------------------------------------------------------
    async def create_payout(
        self,
        *,
        receiver_email: str,
        amount: str,
        currency: str = "USD",
        note: str = "",
        subject: str = "Channel3 agentic payout",
    ) -> dict[str, Any]:
        import uuid

        payload = {
            "sender_batch_header": {
                "sender_batch_id": f"batch-{uuid.uuid4().hex[:12]}",
                "email_subject": subject,
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "amount": {"value": amount, "currency": currency},
                    "receiver": receiver_email,
                    "note": note or subject,
                }
            ],
        }
        resp = await self._authed_request("POST", "/v1/payments/payouts", json=payload)
        if resp.status_code >= 400:
            raise PayPalError(
                f"payout 创建失败: {resp.text[:400]}",
                status_code=resp.status_code,
            )
        return resp.json()