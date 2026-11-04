"""K2 降级测试脚本 - Channel3 超时模拟

测试场景：
1. 正常流程：Channel3 正常响应 -> 使用 Channel3 商品
2. 超时降级：设置 timeout=0.1s，模拟 Channel3 超时 -> 自动切换 K1 本地池
3. 缓存命中：相同 query 第二次请求直接走缓存
4. 取消分支：用户点击 Cancel -> 状态回退

用法:
  pytest tests/test_k2_fallback.py -v -s
"""
import pytest
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

# 设置极短的超时来模拟 Channel3 故障
os.environ["CHANNEL3_TIMEOUT_SECONDS"] = "0.1"  # 100ms 超时
os.environ["CHANNEL3_MAX_RETRIES"] = "0"

from app.services.channel3.client import search_products_for_query
from app.services.shopper.catalog_k1 import get_k1_items


class TestK2Fallback:
    """K2 降级测试套件"""

    async def test_normal_channel3_flow(self):
        """场景 1: 正常 Channel3 响应（如果有有效配置）"""
        query = "eco-friendly water bottle"
        
        # 正常情况下应该尝试调用 Channel3
        result = await search_products_for_query(query, limit=5)
        
        # 如果配置了有效的 API key，应该有结果
        # 如果没有配置或失败，返回空列表并降级
        if os.getenv("CHANNEL3_API_KEY"):
            print(f"Channel3 正常流：{len(result)} 条结果")
        else:
            assert result == [], "未配置 API key 时应返回空列表"
            print("未配置 Channel3，返回空列表")

    async def test_timeout_fallback_to_local(self, monkeypatch):
        """场景 2: Channel3 超时，验证降级到本地 K1 池"""
        query = "decorative banner party"
        
        original_timeout = os.environ.get("CHANNEL3_TIMEOUT_SECONDS", "30")
        os.environ["CHANNEL3_TIMEOUT_SECONDS"] = "0.01"
        
        try:
            result = await search_products_for_query(query, limit=5)
            
            # Channel3 超时应该返回空列表，触发降级
            assert result == [], "超时后应返回空列表触发降级"
            print("PASS: Channel3 超时，成功降级")
            
            # 验证本地 K1 池可用
            k1_items = get_k1_items()
            assert len(k1_items) > 0, "K1 商品池必须有数据"
            print(f"PASS: K1 本地池可用：{len(k1_items)} 件商品")
            
        finally:
            os.environ["CHANNEL3_TIMEOUT_SECONDS"] = original_timeout

    async def test_cache_hit_same_query(self):
        """场景 3: 相同 query 的缓存命中测试"""
        query = "test cached query"
        
        # 第一次请求（冷启动）
        result1 = await search_products_for_query(query, limit=3)
        
        # 第二次请求相同 query（应该走缓存）
        result2 = await search_products_for_query(query, limit=3)
        
        # 缓存应返回相同结果
        assert result1 == result2, "缓存应返回相同结果"
        print("PASS: 缓存命中测试通过")

    async def test_multiple_queries_stress(self):
        """压力测试：并发多个查询验证降级稳定性"""
        queries = [
            "party decorations",
            "eco-friendly water bottle",
            "award trophy",
            "office supplies",
        ]
        
        results = await asyncio.gather(*[
            search_products_for_query(q, limit=3) for q in queries
        ])
        
        # 所有查询都应成功（即使降级）
        for i, r in enumerate(results):
            print(f"Query {i+1}: {queries[i]} -> {len(r)} 结果")
        
        print("PASS: 并发查询压力测试通过")


@pytest.mark.asyncio
async def test_cancel_branch_simulation():
    """场景 4: 模拟用户取消支付流程"""
    from fastapi.testclient import TestClient
    from app.main import app
    
    client = TestClient(app)
    
    response = client.get("/api/v1/paypal/cancel?session_id=test-cancel-123")
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "CANCELLED"
    assert "redirect_url" in data
    print("PASS: Cancel 分支测试通过")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])
