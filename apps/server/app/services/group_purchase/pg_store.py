"""PostgreSQL 存储后端 —— 订单档案与审计日志的结构化落盘（双写模式）。

设计目标（简历可讲、生产可用、演进不破坏现有契约）：
  - **双写**：JSONL 仍保留为审计底稿（追加写、人可读、现有 337 个测试的契约），
    PG 作为可查询的结构化副本。两边同时写，PG 挂了不影响 JSONL 主链路。
  - **配置开关**：`DATABASE_URL` 设置了才启用 PG；未设置（CI/离线测试）时
    所有函数都是 no-op / 返回空 —— 纯 JSONL 模式，行为与改造前完全一致。
  - **读优先 PG**：PG 可用时从 PG 读（支持按 session/event 过滤的 SQL 查询），
    否则回落 JSONL 全量扫盘。
  - **建表幂等**：`CREATE TABLE IF NOT EXISTS`，启动时调用一次即可。

表结构：
  orders        —— 每个 order_id 一行（upsert，最后写入为准），items/transitions 存 JSONB
  audit_events  —— 追加写（id 自增），data/constraint_snapshot/plan_snapshot 存 JSONB
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

from psycopg import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL DEFAULT '',
    total           NUMERIC(12, 2) NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'USD',
    plan_id         TEXT,
    paypal_token    TEXT,
    items           JSONB NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'draft',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    cancelled_reason TEXT,
    transitions     JSONB NOT NULL DEFAULT '[]',
    expires_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders (session_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

CREATE TABLE IF NOT EXISTS audit_events (
    id                  BIGSERIAL PRIMARY KEY,
    timestamp           TEXT NOT NULL,
    round_num           INTEGER,
    event               TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    user_id             TEXT NOT NULL DEFAULT '',
    actor               TEXT NOT NULL DEFAULT '',
    data                JSONB NOT NULL DEFAULT '{}',
    constraint_snapshot JSONB,
    plan_snapshot       JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events (session_id);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_events (event);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events (timestamp);
"""


def get_database_url() -> str | None:
    """返回 DATABASE_URL；未配置时返回 None（纯 JSONL 模式）。"""
    url = os.getenv("DATABASE_URL", "").strip()
    return url or None


def pg_enabled() -> bool:
    """PG 是否启用（配置了 DATABASE_URL 即可用）。"""
    return get_database_url() is not None


def _connect():
    """短连接（数据量小：订单数百行、审计数千行，无需连接池）。"""
    url = get_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL 未配置，PG 后端不可用")
    return connect(url, row_factory=dict_row)


def ensure_schema() -> bool:
    """幂等建表。返回是否成功（PG 未配置/连不上 → False，不影响 JSONL 模式）。"""
    if not pg_enabled():
        return False
    try:
        with _connect() as conn:
            conn.execute(_SCHEMA_SQL)
            conn.commit()
        return True
    except Exception as exc:  # pragma: no cover - PG 故障不应阻断启动
        logger.warning("ensure_schema failed: %s", exc)
        return False


# ============================================================
# orders：upsert（同一 order_id 最后写入为准，与 JSONL 语义一致）
# ============================================================


def upsert_order(order: dict[str, Any]) -> bool:
    """把一条订单记录写入 PG（幂等 upsert）。失败只警告，不抛。"""
    if not pg_enabled():
        return False
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, session_id, user_id, total, currency, plan_id,
                    paypal_token, items, status, created_at, updated_at,
                    cancelled_reason, transitions, expires_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (order_id) DO UPDATE SET
                    session_id = EXCLUDED.session_id,
                    user_id = EXCLUDED.user_id,
                    total = EXCLUDED.total,
                    currency = EXCLUDED.currency,
                    plan_id = EXCLUDED.plan_id,
                    paypal_token = EXCLUDED.paypal_token,
                    items = EXCLUDED.items,
                    status = EXCLUDED.status,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    cancelled_reason = EXCLUDED.cancelled_reason,
                    transitions = EXCLUDED.transitions,
                    expires_at = EXCLUDED.expires_at
                """,
                (
                    order["order_id"], order.get("session_id", ""),
                    order.get("user_id", ""), order.get("total", 0),
                    order.get("currency", "USD"), order.get("plan_id"),
                    order.get("paypal_token"), Jsonb(order.get("items") or []),
                    order.get("status", "draft"), order.get("created_at", ""),
                    order.get("updated_at", ""), order.get("cancelled_reason"),
                    Jsonb(order.get("transitions") or []), order.get("expires_at"),
                ),
            )
            conn.commit()
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("upsert_order failed for %s: %s", order.get("order_id"), exc)
        return False


def load_orders() -> list[dict[str, Any]]:
    """全量读订单（时间正序），供启动恢复。PG 未启用 → 空列表。"""
    if not pg_enabled():
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY created_at, order_id"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # pragma: no cover
        logger.warning("load_orders failed: %s", exc)
        return []


# ============================================================
# audit_events：追加写 + 按 session/event 过滤读
# ============================================================


def insert_audit(entry: dict[str, Any]) -> bool:
    """把一条审计事件写入 PG。失败只警告，不抛。"""
    if not pg_enabled():
        return False
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (
                    timestamp, round_num, event, session_id, user_id, actor,
                    data, constraint_snapshot, plan_snapshot
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    entry.get("timestamp", ""), entry.get("round_num", 0),
                    entry.get("event", ""), entry.get("session_id", ""),
                    entry.get("user_id", ""), entry.get("actor", ""),
                    Jsonb(entry.get("data") or {}),
                    Jsonb(entry.get("constraint_snapshot"))
                    if entry.get("constraint_snapshot") is not None else None,
                    Jsonb(entry.get("plan_snapshot"))
                    if entry.get("plan_snapshot") is not None else None,
                ),
            )
            conn.commit()
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("insert_audit failed: %s", exc)
        return False


def load_audit(
    session_id: str | None = None,
    user_id: str | None = None,
    event_type: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """按条件读审计（时间正序，最近 limit 条）。未启用 → 空列表。"""
    if not pg_enabled():
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if session_id:
        clauses.append("session_id = %s")
        params.append(session_id)
    if user_id:
        clauses.append("user_id = %s")
        params.append(user_id)
    if event_type:
        clauses.append("event = %s")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_events {where} ORDER BY timestamp, id LIMIT %s",
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # pragma: no cover
        logger.warning("load_audit failed: %s", exc)
        return []


def migrate_jsonl_to_pg(records: Iterable[dict[str, Any]]) -> int:
    """把 JSONL 恢复出的对象批量写入 PG（迁移/对账用）。返回成功条数。"""
    if not pg_enabled():
        return 0
    n = 0
    for rec in records:
        if upsert_order(rec):
            n += 1
    return n


def insert_audit_if_absent(entry: dict[str, Any]) -> str:
    """迁移专用幂等审计写入：先查重再插入（重复跑不产生重复行）。

    普通运行时双写走 insert_audit（append-only，无查重开销）；
    这个查重版本只给 scripts/migrate_jsonl_to_pg.py 用，
    保证把同一批 JSONL 迁移两次不会在 audit_events 里出现重复行。

    查重键：timestamp + session_id + event + user_id + actor + data + 两个快照
    —— 快照也必须纳入：同一事件 data 可能相同但 plan/constraint 快照不同，
    漏了会把「带快照的新行」误判成已存在而跳过（数据丢失）。

    返回三态（供迁移脚本分开计数对账）：
      "inserted" —— 新插入
      "exists"   —— 已存在（幂等跳过）
      "failed"   —— 写入失败（PG 不可用 / 异常）
    """
    if not pg_enabled():
        return "failed"
    try:
        with _connect() as conn:
            exists = conn.execute(
                """
                SELECT 1 FROM audit_events
                WHERE timestamp = %s AND session_id = %s AND event = %s
                  AND user_id = %s AND actor = %s AND data = %s
                  AND constraint_snapshot IS NOT DISTINCT FROM %s
                  AND plan_snapshot IS NOT DISTINCT FROM %s
                LIMIT 1
                """,
                (
                    entry.get("timestamp", ""), entry.get("session_id", ""),
                    entry.get("event", ""), entry.get("user_id", ""),
                    entry.get("actor", ""),
                    Jsonb(entry.get("data") or {}),
                    Jsonb(entry.get("constraint_snapshot"))
                    if entry.get("constraint_snapshot") is not None else None,
                    Jsonb(entry.get("plan_snapshot"))
                    if entry.get("plan_snapshot") is not None else None,
                ),
            ).fetchone()
            if exists:
                return "exists"  # 已存在：幂等迁移跳过，不计为失败
            conn.execute(
                """
                INSERT INTO audit_events (
                    timestamp, round_num, event, session_id, user_id, actor,
                    data, constraint_snapshot, plan_snapshot
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    entry.get("timestamp", ""), entry.get("round_num", 0),
                    entry.get("event", ""), entry.get("session_id", ""),
                    entry.get("user_id", ""), entry.get("actor", ""),
                    Jsonb(entry.get("data") or {}),
                    Jsonb(entry.get("constraint_snapshot"))
                    if entry.get("constraint_snapshot") is not None else None,
                    Jsonb(entry.get("plan_snapshot"))
                    if entry.get("plan_snapshot") is not None else None,
                ),
            )
            conn.commit()
        return "inserted"
    except Exception as exc:  # pragma: no cover
        logger.warning("insert_audit_if_absent failed: %s", exc)
        return "failed"


def json_dumps(obj: Any) -> str:
    """兼容 JSON 序列化辅助（供迁移脚本/调试使用）。"""
    return json.dumps(obj, ensure_ascii=False)