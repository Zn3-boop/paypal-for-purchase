"""会话持久化回归 —— 后端重启后，约束 / 方案 / 购物车 / 订单号必须还在。

契约（session_state）：
  - update_session 原子写 data/sessions/<safe>.json（SESSION_STORE_DIR 可覆盖）；
  - 内存被清空（= 进程重启）后，get_session 能从磁盘懒恢复；
  - 启动时 load_sessions_from_disk 回放所有会话，且不覆盖内存里更新的会话；
  - 删除会话会同时删文件；损坏 / 非法文件只跳过，不让服务起不来。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

from app.services.group_purchase import session_state as ss
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.session_state import (
    PlanItem,
    ShoppingPlan,
    get_or_create_session,
    get_session,
    update_session,
    delete_session,
    load_sessions_from_disk,
    get_session_file,
)


def _seed(session_id: str = "sess-restart"):
    session = get_or_create_session(session_id, "user-1")
    session.intent_query = "社团采购 户外 装饰"
    session.intent_mode = "group"
    session.state = "reviewing"
    session.constraints = [
        Constraint(
            id="c1",
            type="budget",
            content="总预算 500 美元",
            hard=True,
            source="成员 A",
            numeric_value=500.0,
        )
    ]
    item = PlanItem(id="i1", name="帆布横幅", category="装饰", price=99.5)
    session.cart_items = [item]
    session.selected_plan = ShoppingPlan(
        plan_id="A",
        strategy="低价优先",
        items=[item],
        total=99.5,
        source="channel3",
    )
    session.order_id = "ord_persist_1"
    session.order_status = "draft"
    update_session(session)
    return session


def test_session_survives_memory_wipe():
    _seed()

    # 模拟进程重启：内存全空，只剩磁盘 JSON
    ss._sessions.clear()

    restored = get_session("sess-restart")
    assert restored is not None
    assert restored.intent_query == "社团采购 户外 装饰"
    assert restored.intent_mode == "group"
    assert restored.state == "reviewing"
    assert [c.id for c in restored.constraints] == ["c1"]
    assert restored.constraints[0].numeric_value == 500.0
    assert restored.cart_items[0].name == "帆布横幅"
    assert restored.selected_plan is not None
    assert restored.selected_plan.source == "channel3"
    assert restored.order_id == "ord_persist_1"
    assert restored.order_status == "draft"


def test_startup_load_restores_all_sessions():
    _seed("sess-a")
    _seed("sess-b")
    ss._sessions.clear()

    assert load_sessions_from_disk() == 2
    assert set(ss._sessions) >= {"sess-a", "sess-b"}


def test_startup_load_does_not_clobber_live_session():
    _seed("sess-live")
    live = ss._sessions["sess-live"]
    live.intent_query = "内存里更新的最新需求"

    load_sessions_from_disk()

    assert get_session("sess-live").intent_query == "内存里更新的最新需求"


def test_delete_session_removes_file():
    _seed("sess-del")
    path = get_session_file("sess-del")
    assert path.exists()

    assert delete_session("sess-del") is True
    assert not path.exists()
    ss._sessions.clear()
    assert get_session("sess-del") is None


def test_corrupt_session_file_is_skipped():
    _seed("sess-bad")
    path = get_session_file("sess-bad")
    path.write_text("{not json", encoding="utf-8")
    ss._sessions.clear()

    assert get_session("sess-bad") is None
    assert load_sessions_from_disk() == 0


def test_session_filename_is_sanitized():
    path = get_session_file("../../etc/passwd")
    assert path.parent == ss.get_sessions_dir()
    assert ".." not in path.name and "/" not in path.name and "\\" not in path.name


def test_persisted_file_keeps_plan_snapshot_and_auto_ids():
    session = get_or_create_session("sess-snapshot", "u2")
    session.auto_constraint_ids = ["c9"]
    session.plans_snapshot = {"feasible": True, "plans": [{"plan_id": "A"}]}
    update_session(session)

    payload = json.loads(get_session_file("sess-snapshot").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["auto_constraint_ids"] == ["c9"]
    assert payload["plans_snapshot"]["feasible"] is True
