"""Channel3 检索记忆 API —— 「搜过什么 / 选过什么」的只读快捷入口。

聚合维度：去重后的最近检索记录（含次数）、最近选过的方案（含总额/件数/检索词）。
只读 + 不写审计（这是纯 UI 辅助信息，不属于决策链路）。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.group_purchase.search_memory import recent_searches, recent_selections

router = APIRouter(prefix="/search-memory", tags=["search-memory"])


@router.get("")
async def get_search_memory(
    limit: int = Query(8, ge=1, le=20),
    selections_limit: int = Query(5, ge=0, le=10),
) -> dict:
    """返回团队级检索记忆（本地演示无多租户隔离，README 已声明）。"""
    return {
        "recent_searches": recent_searches(limit),
        "recent_selections": recent_selections(selections_limit),
    }