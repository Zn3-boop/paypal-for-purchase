"""会话生命周期回归 —— 持久化状态必须有时钟，不能永生。

问题来源（真实体验）：浏览器 localStorage 里的 session_id 永久复用 + 磁盘 JSON
永久存在，于是"同一个 demo-user 隔天回来，旧约束/旧方案/旧购物车原地复活"。
换 user_id / 校验属主都治不了它 —— 因为 user_id 没变，被复用是**时间**问题。

修复方式参考 Django Session：不是在读之外跑定时清理，而是**读的时候就不返回**。
本文件把这条契约钉死：

1. 空闲超过 TTL 的会话：`get_session` 直接返回 None 并删除磁盘文件；
2. 过期会话被 `get_or_create_session` 拿到时：重建为空会话（旧约束不复活）；
3. 磁盘恢复路径同样判过期（重启后不能把过期会话重新加载进内存）；
4. 老文件没有 `last_active_at` 时按 `updated_at` 兜底（升级即清理真实残留）；
5. 活跃即续命（滑动窗口）：读一次不会把"还剩 10 秒"的会话踢掉；
6. `SESSION_TTL_SECONDS<=0` 关闭过期（长演示留一条后路）；
7. `/session/{id}/status` 回显 ttl/剩余秒数，前端倒计时不必自己猜。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.group_purchase import order_lifecycle as ol
from app.services.group_purchase import session_state as ss
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.session_state import (
    get_or_create_session,
    get_session,
    get_session_expires_in_seconds,
    get_session_file,
    get_session_ttl_seconds,
    is_session_expired,
    load_sessions_from_disk,
    save_session_to_disk,
    update_session,
)

TTL = 3600


@pytest.fixture(autouse=True)
def _reset_lifecycle_state(monkeypatch):
    """每个用例：干净的内存表 + 明确的 TTL（不依赖 .env）。"""
    ss._sessions.clear()
    monkeypatch.setenv("SESSION_TTL_SECONDS", str(TTL))
    yield
    ss._sessions.clear()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed(session_id: str, idle_seconds: float = 0) -> None:
    """建一个带约束的会话，并把"最后活跃"调到 idle_seconds 之前。

    注意顺序：`update_session` 会把 last_active_at 刷新成 now（写即活跃），
    所以必须**先** update、**后**改时间戳，再用 save_session_to_disk 落盘
    （save 不做 touch，不会覆盖回去）。
    """
    session = get_or_create_session(session_id, "demo-user")
    session.constraints = [
        Constraint(
            id="c1",
            type="budget",
            content="总预算 300 美元",
            hard=True,
            source="user",
            numeric_value=300.0,
        )
    ]
    session.state = "reviewing"
    update_session(session)
    session.last_active_at = datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)
    save_session_to_disk(session)


def _force_idle_on_disk(session_id: str, idle_seconds: float) -> None:
    """直接改磁盘 JSON 的 last_active_at（模拟"隔天回来"的残留文件）。"""
    path = get_session_file(session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["last_active_at"] = (datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)).isoformat()
    # updated_at 也一起调，确保用例覆盖的是 last_active_at 而不是兜底字段
    payload["updated_at"] = payload["last_active_at"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_fresh_session_is_not_expired():
    _seed("sess-fresh")
    session = get_session("sess-fresh")
    assert session is not None
    assert is_session_expired(session) is False
    assert get_session_expires_in_seconds(session) > TTL - 60


def test_idle_session_expires_on_read_and_file_is_removed():
    _seed("sess-idle", idle_seconds=TTL + 5)
    path = get_session_file("sess-idle")
    assert path.exists()

    assert get_session("sess-idle") is None
    assert "sess-idle" not in ss._sessions
    assert not path.exists(), "过期会话必须连磁盘文件一起清掉，否则目录会无限长"


def test_expired_session_is_recreated_empty_not_resurrected():
    _seed("sess-again", idle_seconds=TTL + 60)

    # 跨天回来：同一个 session_id + 同一个 user_id，旧约束绝不能复活
    session = get_or_create_session("sess-again", "demo-user")
    assert session.constraints == []
    assert session.plans_snapshot is None
    assert session.cart_items == []
    assert session.state == "constraint_collection"


def test_disk_restore_path_also_checks_ttl():
    _seed("sess-restart", idle_seconds=0)
    _force_idle_on_disk("sess-restart", idle_seconds=TTL + 30)
    ss._sessions.clear()  # 模拟进程重启：内存空了，只剩磁盘

    assert get_session("sess-restart") is None
    assert "sess-restart" not in ss._sessions


def test_startup_load_skips_and_deletes_expired_files():
    _seed("sess-live", idle_seconds=0)
    _seed("sess-stale", idle_seconds=0)
    _force_idle_on_disk("sess-stale", idle_seconds=TTL + 30)
    stale_path = get_session_file("sess-stale")
    ss._sessions.clear()

    restored = load_sessions_from_disk()

    assert restored == 1
    assert "sess-live" in ss._sessions
    assert "sess-stale" not in ss._sessions
    assert not stale_path.exists()


def test_legacy_file_without_last_active_at_falls_back_to_updated_at():
    """升级前的会话文件没有 last_active_at：按 updated_at 判真实空闲时长。"""
    _seed("sess-legacy", idle_seconds=0)
    path = get_session_file("sess-legacy")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("last_active_at", None)
    payload["updated_at"] = (datetime.now(timezone.utc) - timedelta(seconds=TTL + 60)).isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    ss._sessions.clear()

    assert get_session("sess-legacy") is None  # 残留文件在升级后第一次读到即被清理


def test_legacy_file_without_timestamps_is_kept():
    """两者都没有（更老/手写数据）时视为刚活跃，不能误杀。"""
    _seed("sess-ancient", idle_seconds=0)
    path = get_session_file("sess-ancient")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("last_active_at", None)
    payload.pop("updated_at", None)
    payload.pop("created_at", None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    ss._sessions.clear()

    restored = get_session("sess-ancient")
    assert restored is not None
    assert is_session_expired(restored) is False


def test_reading_does_not_extend_ttl():
    """**核心回归**：读（含轮询 status / GET plans / GET cart）绝不续期。

    如果读也 touch，前端 3~5 秒一次的轮询会把 TTL 永久保活 ——
    "做了 TTL 又用轮询废掉它"，比不做更糟。
    """
    _seed("sess-readonly", idle_seconds=TTL - 10)
    before = ss._sessions["sess-readonly"].last_active_at

    # 模拟轮询：连续多次只读
    for _ in range(5):
        assert get_session("sess-readonly") is not None

    assert ss._sessions["sess-readonly"].last_active_at == before, "读不得刷新 last_active_at"
    assert get_session_expires_in_seconds(ss._sessions["sess-readonly"]) <= 11


def test_polling_status_never_keeps_session_alive(client):
    """端到端：反复打 /status 不能让会话永生。"""
    _seed("sess-poll", idle_seconds=TTL - 5)
    path = get_session_file("sess-poll")
    before_mtime = path.stat().st_mtime_ns

    for _ in range(3):
        payload = client.get("/api/v1/session/sess-poll/status").json()
        assert payload["exists"] is True

    # 文件没被重写 → last_active_at 没被刷
    assert path.stat().st_mtime_ns == before_mtime
    # 剩余时间仍在倒数，而不是回到 TTL
    assert payload["expires_in_seconds"] <= 6


def test_write_extends_ttl():
    """写（update_session）才续期 —— 提交需求/改购物车/选方案都走这条路。"""
    _seed("sess-write", idle_seconds=TTL - 10)
    session = get_session("sess-write")
    assert session is not None

    update_session(session)  # 写

    assert get_session_expires_in_seconds(ss._sessions["sess-write"]) > TTL - 5



def test_touch_does_not_rewrite_file_on_every_read():
    """读路径不能写盘（前端 3~5 秒轮询一次，全量重写会话 JSON 是纯浪费）。"""
    _seed("sess-noio", idle_seconds=0)
    path = get_session_file("sess-noio")
    before = path.stat().st_mtime_ns

    for _ in range(3):
        get_session("sess-noio")

    assert path.stat().st_mtime_ns == before, "读操作不应触发落盘"


def test_ttl_zero_disables_expiry(monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "0")
    _seed("sess-forever", idle_seconds=TTL * 24)
    session = get_session("sess-forever")
    assert session is not None
    assert get_session_expires_in_seconds(session) is None
    assert get_session_ttl_seconds() == 0


def test_status_endpoint_exposes_countdown_fields(client):
    _seed("sess-status", idle_seconds=30)
    payload = client.get("/api/v1/session/sess-status/status").json()

    assert payload["exists"] is True
    assert payload["ttl_seconds"] == TTL
    assert 0 < payload["expires_in_seconds"] <= TTL
    assert payload["last_active_at"]


def test_status_endpoint_reports_expired_session_as_missing(client):
    _seed("sess-gone", idle_seconds=TTL + 60)
    payload = client.get("/api/v1/session/sess-gone/status").json()

    assert payload["exists"] is False


def test_save_session_to_disk_keeps_last_active_at():
    """落盘不改变 last_active_at —— 续期只由写路径的 update_session 决定。"""
    session = get_or_create_session("sess-bookkeep", "demo-user")
    stamp = session.last_active_at
    assert save_session_to_disk(session) is True
    assert session.last_active_at == stamp


def test_default_store_paths_do_not_depend_on_cwd(monkeypatch):
    """默认数据目录必须锚定 apps/server/data，不能随启动目录漂移。

    事故：从仓库根启动读 `data/*`，从 apps/server 启动读 `apps/server/data/*`，
    两个目录都有旧文件时，排查看到的和进程读的不是同一份（TTL 像没生效）。
    """
    from app.paths import SERVER_ROOT, data_dir

    for env_var in ("SESSION_STORE_DIR", "ORDER_STORE_DIR", "AUDIT_LOG_STORE_DIR",
                    "SEARCH_MEMORY_DIR", "GRAPH_MEMORY_STORE_DIR"):
        monkeypatch.delenv(env_var, raising=False)

    assert data_dir("SESSION_STORE_DIR", "sessions") == SERVER_ROOT / "data" / "sessions"
    assert data_dir("SESSION_STORE_DIR", "sessions").is_absolute()

    # 相对值也按 SERVER_ROOT 解释，而不是 cwd
    monkeypatch.setenv("SESSION_STORE_DIR", "data/sessions")
    assert data_dir("SESSION_STORE_DIR", "sessions") == SERVER_ROOT / "data" / "sessions"

    # 绝对路径原样使用（测试/部署）
    monkeypatch.setenv("SESSION_STORE_DIR", str(SERVER_ROOT.parent / "elsewhere"))
    assert data_dir("SESSION_STORE_DIR", "sessions") == SERVER_ROOT.parent / "elsewhere"


def _paid_order(session_id: str, total: float, order_id_suffix: str = "") -> "ol.OrderRecord":
    """构造一个真正走到 paid 的订单：draft → confirmed → paying → paid。

    复核守卫要求审计里有 review_confirmed，所以先写一条（与真实链路一致），
    而不是绕过守卫直接改 status —— 这样测试验证的是生产路径。
    """
    from app.services.group_purchase.audit_log import get_audit_logger

    order = ol.create_order_record(
        session_id=session_id,
        user_id="demo-user",
        total=total,
        items=[{"id": f"i1{order_id_suffix}", "name": "保温杯", "category": "杯具",
                "price": total, "quantity": 1}],
    )
    get_audit_logger().log(
        session_id=session_id,
        user_id="demo-user",
        event="review_confirmed",
        actor="user",
        data={"order_id": order.order_id, "approved": True},
    )
    ok, msg = ol.confirm_order(order, actor="user")
    assert ok is True, msg
    ok, msg = ol.begin_payment(order, actor="user")
    assert ok is True, msg
    ok, msg = ol.complete_payment(order, captured_amount=total, actor="paypal_callback")
    assert ok is True, msg
    assert order.status == ol.OrderStatus.PAID
    return order


def test_paid_order_survives_session_expiry():
    """已支付订单是「事实」，不能跟着会话一起过期。

    会话是工作台（约束/方案/购物车/order_id 指针），可过期、可重建；
    订单是 append-only 的事实记录（data/orders/*.jsonl），独立存储、独立加载。
    session 过期清理只能删 data/sessions/<id>.json，绝不能碰订单。
    """
    _seed("sess-paid", idle_seconds=0)
    order = _paid_order("sess-paid", 99.0)

    # 会话空闲到远超 TTL，再读一次 → 工作台被清掉
    session = ss._sessions.get("sess-paid")
    assert session is not None
    session.last_active_at = datetime.now(timezone.utc) - timedelta(seconds=TTL + 3600)
    save_session_to_disk(session)

    assert get_session("sess-paid") is None
    assert not get_session_file("sess-paid").exists()

    # 订单必须还在：内存能查到、状态是 paid、读路径不会把它判成 expired
    stored = ol.get_order_record(order.order_id)
    assert stored is not None
    assert stored.status == ol.OrderStatus.PAID
    assert ol.payment_window_closed(stored) is False

    # 重启后（内存订单表清空）依然能从 append-only JSONL 回放出来
    ol._orders.clear()
    assert ol.load_orders_from_disk() >= 1
    replayed = ol.get_order_record(order.order_id)
    assert replayed is not None and replayed.status == ol.OrderStatus.PAID


def test_paid_order_is_listed_even_after_session_recreated(client):
    """重建会话后，档案页仍按 session_id 查得到历史已支付订单。"""
    _seed("sess-archive", idle_seconds=0)
    order = _paid_order("sess-archive", 50.0)

    # 让会话过期 → 前端 ensureSession 会用同一个 id 重建空会话
    ss._sessions["sess-archive"].last_active_at = datetime.now(timezone.utc) - timedelta(seconds=TTL + 60)
    save_session_to_disk(ss._sessions["sess-archive"])
    assert get_session("sess-archive") is None
    rebuilt = get_or_create_session("sess-archive", "demo-user")
    assert rebuilt.constraints == []  # 工作台是干净的

    archive = client.get("/api/v1/orders", params={"session_id": "sess-archive"}).json()
    assert archive["count"] == 1
    assert archive["orders"][0]["order_id"] == order.order_id
    assert archive["orders"][0]["status"] == "paid"

