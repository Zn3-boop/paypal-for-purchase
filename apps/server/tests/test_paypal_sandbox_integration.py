"""G3 沙盒真实联调测试"""
import pytest
import os
from dotenv import load_dotenv

# Load .env before importing
load_dotenv()

from app.services.paypal_gateway import get_paypal_gateway

@pytest.mark.asyncio
async def test_paypal_sandbox_order_creation():
    """测试创建 PayPal Sandbox 订单 (1 美元)。"""
    gateway = get_paypal_gateway()
    
    # 创建订单 - 只验证能成功建立连接并返回 order_id
    result = gateway.create_order(1.00, "ECV Group Purchase Demo")
    
    assert result["order_id"] is not None
    assert result["status"] == "CREATED"
    assert "approval_url" in result
    assert result["approval_url"] is not None
    print(f"Order created: {result['order_id']}")
    print(f"Approval URL: {result['approval_url']}")

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
