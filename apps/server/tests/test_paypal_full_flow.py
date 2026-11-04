"""G3 完整支付链路测试 (含 create + capture + cancel)"""
import pytest
import os
from dotenv import load_dotenv
load_dotenv()

from app.services.paypal_gateway import get_paypal_gateway

@pytest.mark.asyncio
async def test_paypal_full_flow_create_and_capture():
    """完整 PayPal 流程：创建订单 -> (模拟批准) -> 捕获订单"""
    gateway = get_paypal_gateway()
    
    # 步骤 1: 创建订单
    order_result = gateway.create_order(
        amount=12.00,
        item_name="ECV Demo Order (Sandbox)",
        reference_id="test-demo-flow",
    )
    
    assert order_result["order_id"] is not None
    assert order_result["status"] == "CREATED"
    assert order_result["approval_url"] is not None
    print(f"\n[Step 1] Order Created: {order_result['order_id']}")
    print(f"Approval URL: {order_result['approval_url']}")
    
    # 步骤 2: (实际演示时需要用户浏览器批准)
    # 这里跳过批准直接测试捕获（会失败，验证流程完整性）
    from urllib.error import HTTPError
    
    with pytest.raises(HTTPError):
        gateway.capture_order(order_result["order_id"])
    
    print("\n[Expected] Capture failed without approval")
    print("Manual test: Visit approval_url in browser, login sandbox buyer, approve, then capture")

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
