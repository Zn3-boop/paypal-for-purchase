"""PayPal Gateway - Real Payment Flow (Sandbox) using REST API."""
import os
import json
import base64
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

class PayPalGateway:
    def __init__(self):
        # 优先从集中配置读（pydantic-settings 会加载 apps/server/.env）；
        # 回退到 os.getenv（CI / 环境变量注入场景）。
        client_id = ""
        client_secret = ""
        mode = os.getenv("PAYPAL_MODE", "sandbox")
        try:
            from app.core.config import get_settings

            cfg = get_settings()
            client_id = cfg.paypal_sandbox_client_id or ""
            client_secret = cfg.paypal_sandbox_secret or ""
            mode = cfg.paypal_mode or mode
        except Exception as exc:  # pragma: no cover - 配置不可用时回退 env
            logger.warning("读取集中配置失败，回退 os.getenv: %s", exc)

        self.client_id = client_id or os.getenv("PAYPAL_SANDBOX_CLIENT_ID") or os.getenv("PAYPAL_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("PAYPAL_SANDBOX_SECRET") or os.getenv("PAYPAL_CLIENT_SECRET")
        self.mode = mode
        self.return_url = os.getenv("PAYPAL_RETURN_URL", "http://localhost:8000/api/v1/paypal/callback")
        self.cancel_url = os.getenv("PAYPAL_CANCEL_URL", "http://localhost:8000/api/v1/paypal/cancel")
        try:
            from app.core.config import get_settings

            cfg = get_settings()
            self.return_url = cfg.paypal_return_url or self.return_url
            self.cancel_url = cfg.paypal_cancel_url or self.cancel_url
        except Exception:  # pragma: no cover - 配置不可用时用默认值
            pass

        if not self.client_id or not self.client_secret:
            raise ValueError("Missing PAYPAL credentials")

        if self.mode == "sandbox":
            self.base_url = "https://api-m.sandbox.paypal.com"
            self.token_url = "https://api.sandbox.paypal.com/v1/oauth2/token"
        else:
            self.base_url = "https://api-m.paypal.com"
            self.token_url = "https://api.paypal.com/v1/oauth2/token"

    def _get_access_token(self) -> str:
        auth = f"{self.client_id}:{self.client_secret}"
        encoded_auth = base64.b64encode(auth.encode()).decode()
        headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = b"grant_type=client_credentials"
        req = urllib.request.Request(self.token_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())["access_token"]
        except urllib.error.HTTPError as e:
            logger.error("PayPal token error: %s", e.read().decode())
            raise

    def create_order(
        self,
        amount: float,
        item_name: str = "ECV Group Purchase Order",
        return_url: str | None = None,
        cancel_url: str | None = None,
        reference_id: str = "group-purchase-ref",
        currency: str = "USD",
    ):
        access_token = self._get_access_token()
        url = f"{self.base_url}/v2/checkout/orders"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id,
                "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                "description": item_name,
            }],
            "application_context": {
                "brand_name": "ECV Group Purchase",
                "landing_page": "BILLING",
                "user_action": "PAY_NOW",
                "return_url": return_url or self.return_url,
                "cancel_url": cancel_url or self.cancel_url,
            },
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                approval_url = next(
                    (l["href"] for l in result.get("links", []) if l.get("rel") == "approve"),
                    None,
                )
                logger.info("Order Created: %s", result["id"])
                return {
                    "order_id": result["id"],
                    "approval_url": approval_url,
                    "status": result["status"],
                }
        except urllib.error.HTTPError as e:
            logger.error("Create order error: %s", e.read().decode())
            raise

    def get_order(self, order_id: str):
        """查询订单当前状态（用于回调前校验）。"""
        access_token = self._get_access_token()
        url = f"{self.base_url}/v2/checkout/orders/{order_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        return {"order_id": result["id"], "status": result["status"]}

    def capture_order(self, order_id: str):
        access_token = self._get_access_token()
        url = f"{self.base_url}/v2/checkout/orders/{order_id}/capture"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                pu = result["purchase_units"][0]
                capture = pu["payments"]["captures"][0]
                logger.info("Order Captured: %s status=%s", result["id"], result["status"])
                return {
                    "order_id": result["id"],
                    "status": result["status"],
                    "capture_id": capture["id"],
                    "amount": float(capture["amount"]["value"]),
                    "currency": capture["amount"]["currency_code"],
                    "final_capture": capture.get("final_capture", True),
                    "seller_protection": capture.get("seller_protection", {}).get("status", "NONE"),
                    "payer_email": (result.get("payer") or {}).get("email_address", ""),
                }
        except urllib.error.HTTPError as e:
            logger.error("Capture order error: %s", e.read().decode())
            raise


_gateway_instance = None

def get_paypal_gateway() -> PayPalGateway:
    global _gateway_instance
    if _gateway_instance is None:
        _gateway_instance = PayPalGateway()
    return _gateway_instance
