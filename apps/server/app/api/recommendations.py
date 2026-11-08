"""Channel3 推荐 API —— 读侧推导 + 一个被预算护栏约束的加购动作。

GET  /api/v1/recommendations?session_id=...&limit=3
     基于落盘检索/选择记忆让 Channel3 再搜一次，返回带出处（basis / query_used / search_hits）的卡片。
POST /api/v1/recommendations/add
     把某张卡加进购物车；默认超硬预算直接 budget_blocked（购物车不动），force=true 才放行。
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.group_purchase.recommendations import (
    add_recommendation_to_cart,
    build_recommendations,
)
from app.services.group_purchase.session_state import get_session

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


class AddRecommendationRequest(BaseModel):
    session_id: str
    item_id: str
    force: bool = False


@router.get("")
async def get_recommendations(
    session_id: str = Query("", description="可选：用于排除已在购物车的商品与缓存加购项"),
    limit: int = Query(3, ge=1, le=8),
) -> dict:
    session = get_session(session_id) if session_id.strip() else None
    return await build_recommendations(session, limit)


@router.post("/add")
async def add_recommendation(req: AddRecommendationRequest) -> dict:
    return add_recommendation_to_cart(req.session_id, req.item_id, force=req.force)
