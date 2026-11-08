"""团体采购协理集成测试 —— Channel3 vs 本地 catalog 切换。

验证：
  1. Channel3 API 可用时优先使用真实商品
  2. Channel3 失效/无 key 时降级本地 catalog
  3. 约束前置过滤在两种模式下都工作
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from app.services.group_purchase import (
    Constraint,
    generate_dual_plans,
    prefilter_by_constraints,
)
from app.services.channel3 import Channel3Result
from app.services.shopper.catalog import get_catalog_items


def _result(items):
    """把 mock 商品列表包成三态结果：空列表 = empty（检索成功但无匹配）。"""
    return Channel3Result(list(items), "ok" if items else "empty", "")


@pytest.mark.asyncio
async def test_channel3_priority_when_available():
    """Channel3 可用时优先使用真实商品。"""
    # 模拟 Channel3 返回
    mock_channel3_items = [
        {
            "id": "ch3_001",
            "name": "真实横幅 - 环保材质",
            "category": "merch",
            "price": 89.99,
            "currency": "USD",
            "tags": ["环保", "横幅", "定制"],
            "source": "channel3",
        },
    ]
    
    with patch(
        "app.services.channel3.search_products_with_status",
        return_value=_result(mock_channel3_items),
    ):
        constraints = [
            Constraint(
                id="c1",
                type="budget",
                content="预算不超过 500",
                hard=True,
                source="member_A",
                numeric_value=500,
            ),
        ]
        
        result = await generate_dual_plans(
            intent_query="社团活动横幅",
            constraints=constraints,
            use_channel3=True,
        )
        
        assert result["feasible"] is True
        assert result["source"] == "channel3"
        assert result["plan_a"]["items"][0]["source"] == "channel3"


@pytest.mark.asyncio
async def test_fallback_to_local_catalog():
    """Channel3 失败时降级本地 catalog。"""
    # 模拟 Channel3 失败
    with patch(
        "app.services.channel3.search_products_with_status",
        return_value=_result([]),  # 空结果 = empty（检索成功但无匹配）
    ):
        constraints = [
            Constraint(
                id="c1",
                type="budget",
                content="预算不超过 200",
                hard=True,
                source="member_A",
                numeric_value=200,
            ),
        ]
        
        result = await generate_dual_plans(
            intent_query="奖品",
            constraints=constraints,
            use_channel3=True,
        )
        
        assert result["feasible"] is True
        assert result["source"] == "k1_catalog"
        # 验证商品来自本地 catalog（id 不是 ch3_ 开头）
        assert not result["plan_a"]["items"][0]["id"].startswith("ch3_")


@pytest.mark.asyncio
async def test_constraint_prefiltering_works():
    """约束前置过滤在两种模式下都工作。"""
    # 禁忌约束：不要塑料
    constraints = [
        Constraint(
            id="c_taboo",
            type="taboo",
            content="不要塑料制品",
            hard=True,
            source="member_C",
        ),
    ]
    
    # 本地 catalog 中有含"塑料"的商品
    catalog = get_catalog_items()
    plastic_items = [i for i in catalog if "塑料" in str(i.get("tags", []) + [i.get("description", "")])]
    
    if plastic_items:
        filtered = prefilter_by_constraints(catalog, constraints)
        # 验证塑料商品被过滤
        filtered_ids = {i["id"] for i in filtered}
        assert all(pid not in filtered_ids for pid in [p["id"] for p in plastic_items])


@pytest.mark.asyncio
async def test_dual_plan_trade_offs():
    """双方案正确标注取舍。"""
    constraints = [
        Constraint(
            id="c1",
            type="budget",
            content="预算不超过 300",
            hard=True,
            source="member_A",
            numeric_value=300,
        ),
        Constraint(
            id="c2",
            type="preference",
            content="成员 B 喜欢高品质",
            hard=False,
            source="member_B",
        ),
        Constraint(
            id="c3",
            type="preference",
            content="成员 C 想省钱",
            hard=False,
            source="member_C",
        ),
    ]
    
    result = await generate_dual_plans(
        intent_query="社团奖品",
        constraints=constraints,
        use_channel3=False,  # 只用本地 catalog
    )
    
    assert result["feasible"] is True
    
    # 方案 A（低价优先）应该提到牺牲了质量诉求
    plan_a = result["plan_a"]
    plan_b = result["plan_b"]
    
    assert plan_a["strategy"] == "低价优先"
    # 统一思想：B 不再是「另一套策略的另一篮货」，而是 A 的同品类匹配升级
    assert plan_b["strategy"] == "匹配优先"
    
    # 验证总价计算正确
    total_a = sum(item["subtotal"] for item in plan_a["items"])
    assert abs(plan_a["total"] - total_a) < 0.01
    
    total_b = sum(item["subtotal"] for item in plan_b["items"])
    assert abs(plan_b["total"] - total_b) < 0.01
    
    # 预算限制生效
    assert plan_a["total"] <= 300.01
    assert plan_b["total"] <= 300.01


def test_prefilter_handles_empty_catalog():
    """前置过滤后没有商品时的处理。"""
    empty_catalog = []
    constraints = [
        Constraint(
            id="c1",
            type="budget",
            content="预算 1000",
            hard=False,
            source="test",
        ),
    ]
    
    filtered = prefilter_by_constraints(empty_catalog, constraints)
    assert filtered == []