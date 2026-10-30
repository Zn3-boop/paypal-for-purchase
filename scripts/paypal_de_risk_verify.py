"""
PayPal Orders v2 de-risk 验证脚本 (Day 1)

目标：验证 Orders v2 全链路在沙盒可用：
  1. 获取 OAuth token (client_credentials)
  2. create order (intent=CAPTURE) -> 拿到 approval URL
  3. [手动] 用沙盒 personal 账号打开 approval URL 审批
  4. capture order -> 确认 final capture (COMPLETED)

用法：
  python scripts/paypal_de_risk_verify.py
  python scripts/paypal_de_risk_verify.py --order-id XXX --capture

环境变量：PAYPAL_SANDBOX_CLIENT_ID / PAYPAL_SANDBOX_SECRET / PAYPAL_SANDBOX_API
"""
from __future__ import annotations

import argparse
import base64
import json
import os

import httpx

API = os.getenv("PAYPAL_SANDBOX_API", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.getenv("PAYPAL_SANDBOX_CLIENT_ID", "")
SECRET = os.getenv("PAYPAL_SANDBOX_SECRET", "")


def load_env(path: str) -> None:
    """极简 .env 加载（仅本次脚本使用，生产环境走 pydantic-settings）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key.startswith(("PAYPAL_",)):
                if key not in os.environ or not os.environ[key]:
                    os.environ[key] = value


def get_token() -> str:
    """[1/4] 获取 OAuth token。"""
    basic = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    r = httpx.post(
        f"{API}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    token = data["access_token"]
    print(f"[1/4] OAuth token 获取成功 (scope: {data.get('scope','')[:60]}...)")
    return token
def create_order(token: str, amount: str = "25.00") -> dict:
    """[2/4] 创建 CAPTURE 意图订单。"""
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": "channel3-paypal-de-risk",
                "description": "Channel3 Agentic Shopping de-risk 验证订单",
                "amount": {"currency_code": "USD", "value": amount},
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                    "brand_name": "Channel3 Agent",
                    "shipping_preference": "NO_SHIPPING",
                    "user_action": "PAY_NOW",
                    "return_url": "https://example.com/return",
                    "cancel_url": "https://example.com/cancel",
                }
            }
        },
    }
    r = httpx.post(
        f"{API}/v2/checkout/orders",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"[2/4] create order 失败: HTTP {r.status_code}\n{r.text}")
        raise SystemExit(1)
    order = r.json()
    print(f"[2/4] 订单创建成功")
    print(f"      order_id   : {order['id']}")
    print(f"      status     : {order['status']}  (应为 CREATED)")
    links = {x["rel"]: x["href"] for x in order.get("links", [])}
    if "payer-action" in links:
        print(f"      ✅ approval URL 已生成（用沙盒 personal 账号打开审批）:")
        print(f"      {links['payer-action']}")
    else:
        print(f"      ⚠️ 未找到 payer-action 链接，检查返回：")
        print(json.dumps(order, indent=2, ensure_ascii=False))
    return order


def capture_order(token: str, order_id: str) -> None:
    """[4/4] 审批后执行 capture。"""
    print(f"[4/4] 尝试 capture 订单 {order_id} ...")
    r = httpx.post(
        f"{API}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"      ❌ capture 失败: HTTP {r.status_code}\n{r.text}")
        raise SystemExit(1)
    data = r.json()
    print(f"      ✅ capture 响应 status = {data.get('status')}  (应为 COMPLETED)")
    for pu in data.get("purchase_units", []):
        for pay in pu.get("payments", {}).get("captures", []):
            print(f"      capture_id: {pay['id']} | status: {pay['status']} | 金额: "
                  f"{pay['amount'].get('value')} {pay['amount'].get('currency_code')}")
def main() -> None:
    global CLIENT_ID, SECRET
    for p in (
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
        "apps/server/.env",
        ".env",
    ):
        load_env(p)
    CLIENT_ID = os.getenv("PAYPAL_SANDBOX_CLIENT_ID", CLIENT_ID)
    SECRET = os.getenv("PAYPAL_SANDBOX_SECRET", SECRET)
    if not CLIENT_ID or not SECRET:
        print("❌ 缺少 PAYPAL_SANDBOX_CLIENT_ID / PAYPAL_SANDBOX_SECRET")
        raise SystemExit(1)

    parser = argparse.ArgumentParser(description="PayPal Orders v2 de-risk 验证")
    parser.add_argument("--order-id", help="已创建的订单 ID（审批后用于 capture）")
    parser.add_argument("--capture", action="store_true", help="对 --order-id 执行 capture")
    parser.add_argument("--amount", default="25.00", help="沙盒订单金额")
    args = parser.parse_args()

    token = get_token()

    if args.capture:
        if not args.order_id:
            print("❌ --capture 需要 --order-id")
            raise SystemExit(1)
        capture_order(token, args.order_id)
        return

    if args.order_id:
        r = httpx.get(
            f"{API}/v2/checkout/orders/{args.order_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        r.raise_for_status()
        order = r.json()
        print(f"[3/4] 查询订单 {args.order_id}: status={order.get('status')}")
        if order.get("status") == "COMPLETED":
            print("      ✅ 订单已完成（此前已 capture）")
        else:
            print(f"      ⚠️ 订单尚未 COMPLETED，需先用沙盒 personal 账号在 approval URL 审批，"
                  f"然后运行: python scripts/paypal_de_risk_verify.py --order-id {args.order_id} --capture")
        return

    order = create_order(token, args.amount)
    order_id = order["id"]
    print(f"\n[3/4] 下一步（手动，一次即可）:")
    print(f"      1. 浏览器打开上面的 approval URL，用 PAYPAL 沙盒 personal 账号登录并『同意并付款』")
    print(f"      2. 然后运行: python scripts/paypal_de_risk_verify.py --order-id {order_id} --capture")
    print(f"      （若无法手动审批，确认 create + approval URL 生成即完成 Day1 核心验证）")


if __name__ == "__main__":
    main()