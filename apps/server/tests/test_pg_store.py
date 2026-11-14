"""pg_store 单元测试：mock `_connect`，不依赖本地 PG。

约定（与 conftest 一致）：`_isolate_pg_dual_write` 默认删除 DATABASE_URL
（纯 JSONL 模式，落地不联网）；本文件用 `monkeypatch.setenv` 显式激活
PG 后端、配合 `FakeConn` 拦截所有 SQL，测试结束自动还原环境变量 ——
任何用例都不触碰真实数据库。

覆盖：
  - pg_enabled / get_database_url（配置开关）
  - ensure_schema 幂等（CREATE TABLE IF NOT EXISTS）
  - 订单 upsert：ON CONFLICT 覆盖、末尾 params 为最新状态（last-wins）
  - 审计 append + insert_audit_if_absent 三态（inserted / exists / failed）
  - 按 session/user/event 过滤读 + limit
  - PG 未配置（无 DATABASE_URL）时全部 no-op / 空 / "failed"（纯 JSONL 降级）
"""
from __future__ import annotations

import pytest

from app.services.group_purchase import pg_store


class FakeCursor:
    """模拟 psycopg cursor：记录每条 SQL 与参数，返回预设行。"""

    def __init__(self, rows: list | None = None):
        self.rows = rows or []
        self.sqls: list[str] = []
        self.params: list[tuple | list | None] = []

    def execute(self, sql: str, params=None):
        self.sqls.append(sql)
        self.params.append(params)
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    """模拟 psycopg connection：with 块 + commit 计数。"""

    def __init__(self, rows: list | None = None):
        self.cursor = FakeCursor(rows)
        self.commits = 0

    def execute(self, sql: str, params=None):
        return self.cursor.execute(sql, params)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:  # pragma: no cover - no-op 兼容
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False  # 不吞异常


@pytest.fixture
def fake_pg(monkeypatch):
    """激活 PG 后端并把 `_connect` 换成 FakeConn，返回该连接。"""
    conn = FakeConn()
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake:fake@localhost/fake_db")
    monkeypatch.setattr(pg_store, "_connect", lambda: conn)
    return conn


def make_order(oid: str = "ord_x", status: str = "draft", total: float = 9.9) -> dict:
    return {
        "order_id": oid,
        "session_id": "sess-1",
        "user_id": "u1",
        "total": total,
        "currency": "USD",
        "items": [{"sku": "A", "qty": 1}],
        "status": status,
        "created_at": "2026-11-01T00:00:00+00:00",
        "updated_at": "2026-11-01T00:00:00+00:00",
        "transitions": [],
    }


def make_audit(event: str = "plan_generated", ts: str = "2026-11-01T00:00:00+00:00") -> dict:
    return {
        "timestamp": ts,
        "round_num": 1,
        "event": event,
        "session_id": "sess-1",
        "user_id": "u1",
        "actor": "user",
        "data": {"plan_id": "p1"},
        "constraint_snapshot": {"budget": 100},
        "plan_snapshot": None,
    }
# ============================================================
# 配置开关
# ============================================================


def test_get_database_url_none_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pg_store.get_database_url() is None
    assert pg_store.pg_enabled() is False


def test_get_database_url_returns_stripped_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "  postgresql://a:b@localhost/db  ")
    assert pg_store.get_database_url() == "postgresql://a:b@localhost/db"
    assert pg_store.pg_enabled() is True


def test_get_database_url_blank_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    assert pg_store.get_database_url() is None
    assert pg_store.pg_enabled() is False


# ============================================================
# ensure_schema：幂等建表
# ============================================================


def test_ensure_schema_idempotent_twice(fake_pg) -> None:
    assert pg_store.ensure_schema() is True
    assert pg_store.ensure_schema() is True
    # 两次都执行同一份 CREATE TABLE IF NOT EXISTS（幂等 SQL）
    assert fake_pg.commits == 2
    create_sqls = [s for s in fake_pg.cursor.sqls if "CREATE TABLE IF NOT EXISTS orders" in s]
    assert len(create_sqls) == 2
    audit_sqls = [s for s in fake_pg.cursor.sqls if "CREATE TABLE IF NOT EXISTS audit_events" in s]
    assert len(audit_sqls) == 2


def test_ensure_schema_no_pg_returns_false(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pg_store.ensure_schema() is False


def test_ensure_schema_connection_error_returns_false(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake:fake@localhost/fake_db")

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(pg_store, "_connect", boom)
    assert pg_store.ensure_schema() is False


# ============================================================
# 订单 upsert：last-wins 覆盖
# ============================================================


def test_upsert_order_inserts_with_on_conflict(fake_pg) -> None:
    assert pg_store.upsert_order(make_order(oid="ord_1")) is True
    assert fake_pg.commits == 1
    upsert_sqls = [s for s in fake_pg.cursor.sqls if "INSERT INTO orders" in s]
    assert len(upsert_sqls) == 1
    params = fake_pg.cursor.params[0]
    assert params[0] == "ord_1"  # order_id 是首个绑定参数
    assert params[-1] is None  # 同一次调用的最后一个参数是 expires_at（可为 None）


def test_upsert_order_last_status_wins(fake_pg) -> None:
    """同一 order_id 连续 upsert 三次：每次参数都带最新 status/updated_at。"""
    assert pg_store.upsert_order(make_order(oid="ord_2", status="draft")) is True
    assert pg_store.upsert_order(make_order(oid="ord_2", status="confirmed")) is True
    assert pg_store.upsert_order(make_order(oid="ord_2", status="paid")) is True

    upsert_sqls = [s for s in fake_pg.cursor.sqls if "INSERT INTO orders" in s]
    assert len(upsert_sqls) == 3
    status_params = [
        p for s, p in zip(fake_pg.cursor.sqls, fake_pg.cursor.params, strict=False)
        if "INSERT INTO orders" in s
    ]
    # status 是第 9 个绑定参数（下标 8）
    assert [p[8] for p in status_params] == ["draft", "confirmed", "paid"]
    assert status_params[-1][0] == "ord_2"
    assert status_params[-1][8] == "paid"  # last-wins：最新状态落在最终写入


def test_upsert_order_no_pg_returns_false(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pg_store.upsert_order(make_order()) is False


def test_upsert_order_connection_error_returns_false(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake:fake@localhost/fake_db")

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(pg_store, "_connect", boom)
    assert pg_store.upsert_order(make_order()) is False


def test_load_orders_no_pg_empty(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pg_store.load_orders() == []


def test_load_orders_returns_dict_rows(fake_pg) -> None:
    fake_pg.cursor.rows = [
        {"order_id": "ord_1", "status": "draft", "total": 9.9},
        {"order_id": "ord_2", "status": "paid", "total": 12.0},
    ]
    result = pg_store.load_orders()
    assert len(result) == 2
    assert result[1]["status"] == "paid"
    assert fake_pg.cursor.sqls[-1].startswith("SELECT * FROM orders ORDER BY")
# ============================================================
# 审计：append + insert_audit_if_absent 三态
# ============================================================


def test_insert_audit_appends(fake_pg) -> None:
    assert pg_store.insert_audit(make_audit()) is True
    assert fake_pg.commits == 1
    sql = fake_pg.cursor.sqls[0]
    assert "INSERT INTO audit_events" in sql
    p = fake_pg.cursor.params[0]
    assert p[0] == "2026-11-01T00:00:00+00:00"  # timestamp
    assert p[2] == "plan_generated"  # event


def test_insert_audit_no_pg_false(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pg_store.insert_audit(make_audit()) is False


def test_insert_audit_if_absent_inserted(fake_pg) -> None:
    fake_pg.cursor.rows = []  # SELECT 查重无命中 → 插入
    result = pg_store.insert_audit_if_absent(make_audit())
    assert result == "inserted"
    assert fake_pg.commits == 1
    assert len([s for s in fake_pg.cursor.sqls if "INSERT INTO audit_events" in s]) == 1


def test_insert_audit_if_absent_exists_skips(fake_pg) -> None:
    fake_pg.cursor.rows = [{"x": 1}]  # 查重命中（SELECT 1 有行）→ 跳过
    result = pg_store.insert_audit_if_absent(make_audit())
    assert result == "exists"
    assert fake_pg.commits == 0  # 未插入、未 commit
    assert len([s for s in fake_pg.cursor.sqls if "INSERT INTO audit_events" in s]) == 0
    # 查重 WHERE 必须覆盖两个快照列（审脚本修的点：#2）
    where_sql = [s for s in fake_pg.cursor.sqls if "SELECT 1 FROM audit_events" in s][0]
    assert "constraint_snapshot IS NOT DISTINCT FROM" in where_sql
    assert "plan_snapshot IS NOT DISTINCT FROM" in where_sql
    assert "data = %s" in where_sql


def test_insert_audit_if_absent_no_pg_failed(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pg_store.insert_audit_if_absent(make_audit()) == "failed"


def test_insert_audit_if_absent_connection_error_failed(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake:fake@localhost/fake_db")

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(pg_store, "_connect", boom)
    assert pg_store.insert_audit_if_absent(make_audit()) == "failed"


# ============================================================
# 条件读 + 迁移辅助
# ============================================================


def test_load_audit_filters_and_limit(fake_pg) -> None:
    fake_pg.cursor.rows = [{"id": 1, "event": "plan_generated"}]
    result = pg_store.load_audit(
        session_id="sess-9", user_id="u9", event_type="plan_generated", limit=7
    )
    assert len(result) == 1
    where_sql = fake_pg.cursor.sqls[-1]
    assert "session_id = %s" in where_sql
    assert "user_id = %s" in where_sql
    assert "event = %s" in where_sql
    assert "ORDER BY timestamp, id LIMIT %s" in where_sql
    params = fake_pg.cursor.params[-1]
    assert params[:3] == ("sess-9", "u9", "plan_generated")
    assert params[3] == 7


def test_load_audit_no_pg_empty(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pg_store.load_audit() == []


def test_migrate_jsonl_to_pg_counts(fake_pg) -> None:
    records = [make_order("ord_1"), make_order("ord_2"), make_order("ord_3")]
    assert pg_store.migrate_jsonl_to_pg(records) == 3
    assert fake_pg.commits == 3


def test_migrate_jsonl_to_pg_no_pg_zero(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pg_store.migrate_jsonl_to_pg([make_order()]) == 0