"""审计台账 API — I3：前端审计时间轴的数据源。

读取 AuditLogger（JSONL 持久化）中的日志条目，供前端审计面板轮询。
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.group_purchase.audit_log import get_audit_logger

router = APIRouter(prefix="/audit", tags=["audit"])
logger = logging.getLogger(__name__)


class AuditLogsResponse(BaseModel):
    session_id: str
    logs: list[dict]
    total: int


@router.get("/logs", response_model=AuditLogsResponse)
async def get_audit_logs(
    session_id: str = Query(..., description="ECV 会话 ID"),
    event_type: str | None = Query(None, description="可选的事件类型过滤"),
    limit: int = Query(50, ge=1, le=500, description="最多返回条数"),
) -> AuditLogsResponse:
    """按会话读取审计日志（时间正序，最多 limit 条）。

    前端审计时间轴轮询此端点，展示五个关键事件的完整链条：
    plan_generated → constraint_changed → review_confirmed → payment_completed
    以及 cart_modified / conflict_detected / budget_violated 等过程事件。
    """
    audit = get_audit_logger()
    entries = audit.get_logs(
        session_id=session_id,
        event_type=event_type,  # type: ignore[arg-type]
        limit=limit,
    )
    logs = [e.to_dict() for e in entries]
    return AuditLogsResponse(session_id=session_id, logs=logs, total=len(logs))
