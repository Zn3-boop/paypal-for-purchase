"""J 模块兜底集成测试"""
import pytest
from app.services.group_purchase import Constraint, generate_dual_plans
from app.services.group_purchase.cart_modifier import execute_modifications
from app.services.group_purchase.session_state import get_or_create_session, update_session
from app.services.group_purchase.prompts import CartModificationRequest
from app.services.shopper.catalog_k1 import get_k1_items

@pytest.mark.asyncio
async def test_fallback_budget_too_low():
    # 规划器实际消费 K1 商品池（与 shopper/catalog.py 的 C 端 fixture 不同），下限必须取自同一池。
    catalog = get_k1_items()
    min_price = min(float(i.get("price", 999999)) for i in catalog)
    constraints = [
        Constraint(id="c_budget", type="budget",
                   content=f"总预算不超过 {min_price - 1:.2f}",
                   hard=True, source="成员 A",
                   numeric_value=min_price - 1),
    ]
    result = await generate_dual_plans("社团奖品", constraints, use_channel3=False)
    assert result["feasible"] is False
    assert result["plan_a"] is None
    fallback = result["fallback"]
    # 预算低于池内最低单价 -> 只可能是 budget_too_low（pin 死，禁止三选一糊弄）
    assert fallback["status"] == "budget_too_low", fallback["status"]
    assert "预算过低" in fallback["message"], fallback["message"]
    assert fallback["reason"] == "insufficient_budget"
    assert len(fallback["suggestions"]) >= 2
    actions = " ".join(s.get("action", "") for s in fallback["suggestions"])
    assert "提高预算" in actions and "减少采购数量" in actions, actions

@pytest.mark.asyncio
async def test_fallback_message_is_deterministic():
    constraints = [
        Constraint(id="c_budget", type="budget",
                   content="总预算 1", hard=True, source="成员 A",
                   numeric_value=1),
    ]
    r1 = await generate_dual_plans("社团奖品", constraints, use_channel3=False)
    r2 = await generate_dual_plans("社团奖品", constraints, use_channel3=False)
    assert r1["fallback"]["message"] == r2["fallback"]["message"]
    assert r1["fallback"]["status"] == r2["fallback"]["status"]

@pytest.mark.asyncio
async def test_normal_path_has_no_fallback():
    constraints = [
        Constraint(id="c1", type="budget",
                   content="总预算 500", hard=True, source="成员 A",
                   numeric_value=500),
    ]
    result = await generate_dual_plans("社团奖品", constraints, use_channel3=False)
    assert result["feasible"] is True
    assert result.get("fallback") is None
    assert result["plan_a"]["total"] <= 500

@pytest.mark.asyncio
async def test_fallback_after_adding_expensive_item():
    session = get_or_create_session("test-fallback-v2", "user_a")
    session.reset_to_initial()
    catalog = get_k1_items()
    min_price = min(float(i.get("price", 999999)) for i in catalog)
    session.constraints = [
        Constraint(id="c1", type="budget",
                   content=f"总预算不超过 {min_price - 1:.2f}",
                   hard=True, source="成员 A",
                   numeric_value=min_price - 1),
    ]
    session.cart_items = []
    update_session(session)
    expensive = catalog[0]
    result = await execute_modifications(
        session_id="test-fallback-v2", user_id="user_a",
        modifications=[CartModificationRequest(
            modification_type="add_item",
            target_item_id=expensive["id"],
            explanation="加入商品")],
        catalog=catalog)
    assert result["budget_ok"] is False
    assert result["fallback"] is not None
    assert result["fallback"]["status"] == "budget_violated_after_modification"
