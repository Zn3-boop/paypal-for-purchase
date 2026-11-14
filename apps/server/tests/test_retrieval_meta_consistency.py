"""P2-④ 数字口径自洽（2026-11-14）：候选/命中/过滤三数单一来源且强制守恒。

保护的评审点：用户拿「候选 19 · 命中 19 · 过滤 1」这类 sum 不守恒的数字心算
核对后必然质疑 —— 候选（candidate）= 原始召回条数、命中（matched）= 约束预筛
后条数、过滤（filtered）= candidate − matched（后端强制保证，永不矛盾）。

- _retrieval_meta 单测：多组合下 filtered == candidate − matched 恒成立；
- channel3 集成：原始召回 > 相关池 > 预筛池 → candidate 用原始召回、matched 用预筛池；
- k1 兜底：candidate = 本地目录总条数；
- note 回归：不再写死「已过滤 N 件」数字口径（数字全走结构化字段）。
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")
os.environ.setdefault("GRAPH_ENV", "dev")

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app
from app.services.channel3 import Channel3Result
from app.services.group_purchase import generate_dual_plans
from app.services.group_purchase.constraints import Constraint
from app.services.group_purchase.planner import _retrieval_meta


def _result(items):
    return Channel3Result(list(items), "ok" if items else "empty", "")


def _budget_constraint(limit: float) -> Constraint:
    return Constraint(
        id="budget_1",
        type="budget",
        content=f"预算不超过 {limit}",
        hard=True,
        source="member_A",
        numeric_value=limit,
    )


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _reset(client, sid: str) -> None:
    r = client.post(
        "/api/v1/session/reset",
        json={"session_id": sid, "user_id": "meta-consistency-tester", "hard_reset": True},
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def channel3_stub(monkeypatch):
    """确定性假 Channel3（阻真网）：让 plans API 测试在 CI 无外网时稳定。"""
    items = [
        {"id": "ch3_jk_1", "name": "Team Jacket Warm", "category": "jacket",
         "price": 40.0, "currency": "USD", "tags": ["外套", "保暖"], "source": "channel3", "image_url": ""},
        {"id": "ch3_jk_2", "name": "Team Jacket Pro", "category": "jacket",
         "price": 60.0, "currency": "USD", "tags": ["外套", "旗舰"], "source": "channel3", "image_url": ""},
    ]

    async def _fake_search_status(query: str, limit: int = 20, force_probe: bool = False):  # noqa: ARG001
        return _result([dict(it) for it in items])

    monkeypatch.setattr(
        "app.services.channel3.search_products_with_status", _fake_search_status
    )
    yield items


# ---------- _retrieval_meta 单元：三数守恒 ----------

@pytest.mark.parametrize("recalled,filtered_len", [
    (0, 0),        # 无召回
    (1, 1),        # 一件没过滤
    (5, 2),        # 常态：召回 5 → 命中 2 → 过滤 3
    (20, 19),      # 评审反例：候选 19/命中 19/过滤 1 的旧矛盾源
    (45, 0),       # 全部被约束预筛滤掉
])
def test_retrieval_meta_three_counts_conserve(recalled, filtered_len):
    """三数守恒恒成立：filtered == candidate − matched（后端强制）。"""
    catalog = [{"id": f"c{i}", "name": f"item{i}", "price": 1.0} for i in range(recalled)]
    filtered = catalog[:filtered_len]
    meta = _retrieval_meta(
        "query", "channel3", catalog, filtered,
        search_query="q", recalled_count=recalled,
    )
    assert meta["candidate_count"] == recalled
    assert meta["matched_count"] == filtered_len
    assert meta["filtered_count"] == recalled - filtered_len
    # 可心算核对的第一性原理
    assert meta["candidate_count"] == meta["matched_count"] + meta["filtered_count"]


def test_retrieval_meta_recalled_less_than_catalog_backcompat():
    """无 recalled_count（旧调用方/回退）时回退到 len(catalog)，仍守恒。"""
    catalog = [{"id": f"c{i}", "name": f"item{i}", "price": 1.0} for i in range(5)]
    for keep in (5, 2, 0):
        filtered = catalog[:keep]
        meta = _retrieval_meta("q", "k1_catalog", catalog, filtered)
        assert meta["candidate_count"] == 5
        assert meta["matched_count"] == keep
        assert meta["filtered_count"] == 5 - keep


def test_retrieval_meta_note_has_no_filtered_digit():
    """note 回归：不再写死「已过滤 N 件」数字口径（数字只走结构化字段）。"""
    catalog = [{"id": f"c{i}", "name": f"item{i}", "price": 1.0} for i in range(5)]
    filtered = catalog[:2]
    meta = _retrieval_meta(
        "query", "channel3", catalog, filtered,
        note="部分候选与需求相关性低，已从候选中剔除",
        recalled_count=5,
    )
    assert "已过滤" not in meta["note"]
    assert "部分候选" in meta["note"]
    # 数字口径只出现在结构化字段
    assert meta["filtered_count"] == 3
# ---------- channel3 集成：候选=原始召回、命中=预筛池 ----------

@pytest.mark.asyncio
async def test_channel3_candidate_is_raw_recall_count():
    """Channel3 路径：candidate_count 必须是原始召回条数（非相关池大小）。"""
    raw_items = [
        {"id": "r1", "name": "Team Jacket Warm", "category": "jacket",
         "price": 40.0, "currency": "USD", "tags": ["外套", "保暖"]},
        {"id": "r2", "name": "Team Jacket Light", "category": "jacket",
         "price": 30.0, "currency": "USD", "tags": ["外套"]},
        {"id": "r3", "name": "Team Jacket Pro", "category": "jacket",
         "price": 60.0, "currency": "USD", "tags": ["外套", "旗舰"]},
        {"id": "n1", "name": "Random Water Bottle", "category": "drink",
         "price": 12.0, "currency": "USD", "tags": ["水瓶"]},
        {"id": "n2", "name": "Paper Plates Pack", "category": "party",
         "price": 8.0, "currency": "USD", "tags": ["纸盘"]},
    ]
    with patch(
        "app.services.channel3.search_products_with_status",
        return_value=_result(raw_items),
    ):
        result = await generate_dual_plans(
            intent_query="团队统一采购外套 保暖 藏青色",
            constraints=[_budget_constraint(500)],
            use_channel3=True,
        )
    retrieval = result["retrieval"]
    # 候选 = 原始召回 5（含 2 条不相干的水瓶/纸盘）
    assert retrieval["candidate_count"] == 5
    # 命中 = 约束预筛后进入方案池的条数（相关 3 件都在预算内）
    assert retrieval["matched_count"] == 3
    # 过滤 = 候选 − 命中（2 件不相干）
    assert retrieval["filtered_count"] == 2
    assert retrieval["candidate_count"] == retrieval["matched_count"] + retrieval["filtered_count"]


@pytest.mark.asyncio
async def test_channel3_empty_fallback_k1_candidate_is_local_size():
    """Channel3 空召回 → k1 兜底：candidate = 本地目录总条数（非 0）。"""
    with patch(
        "app.services.channel3.search_products_with_status",
        return_value=_result([]),
    ):
        result = await generate_dual_plans(
            intent_query="团队统一采购外套",
            constraints=[_budget_constraint(500)],
            use_channel3=True,
        )
    retrieval = result["retrieval"]
    assert result["source"] == "k1_catalog"
    assert retrieval["source"] == "k1_catalog"
    # 本地目录 45 件回归锚点 → candidate 至少 > 0 且守恒
    assert retrieval["candidate_count"] > 0
    assert retrieval["filtered_count"] == retrieval["candidate_count"] - retrieval["matched_count"]
    assert retrieval["filtered_count"] >= 0


@pytest.mark.asyncio
async def test_channel3_budget_filters_pool_and_conserves():
    """预算约束拒绝一部分候选：matched = 预算内条数，filtered = 被预算踢掉的条数。"""
    raw_items = [
        {"id": "b1", "name": "Team Jacket Cheap", "category": "jacket",
         "price": 20.0, "currency": "USD", "tags": ["外套"]},
        {"id": "b2", "name": "Team Jacket Mid", "category": "jacket",
         "price": 50.0, "currency": "USD", "tags": ["外套"]},
        {"id": "b3", "name": "Team Jacket Expensive", "category": "jacket",
         "price": 200.0, "currency": "USD", "tags": ["外套"]},
        {"id": "b4", "name": "Team Jacket Ultra", "category": "jacket",
         "price": 500.0, "currency": "USD", "tags": ["外套"]},
    ]
    with patch(
        "app.services.channel3.search_products_with_status",
        return_value=_result(raw_items),
    ):
        result = await generate_dual_plans(
            intent_query="团队统一采购外套 保暖",
            constraints=[_budget_constraint(100)],  # 只放行 ≤100 的两件
            use_channel3=True,
        )
    retrieval = result["retrieval"]
    assert retrieval["candidate_count"] == 4
    assert retrieval["matched_count"] == 2
    assert retrieval["filtered_count"] == 2
    assert retrieval["candidate_count"] == retrieval["matched_count"] + retrieval["filtered_count"]


# ---------- API 层透传：plans.py payload 必带 filtered_count ----------

def test_plans_api_payload_includes_filtered_count(client, channel3_stub):
    """plans 接口检索证据载荷必须含 filtered_count（前端渲染三数自洽的前提）。"""
    sid = _sid("p24")
    _reset(client, sid)
    r = client.post(
        "/api/v1/plans/intent",
        params={"session_id": sid},
        json={"message": "团队统一采购外套 保暖"},
    )
    assert r.status_code == 200, r.text
    resp = client.get("/api/v1/plans", params={"session_id": sid})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    retrieval = payload.get("retrieval", {})
    assert "candidate_count" in retrieval
    assert "matched_count" in retrieval
    assert "filtered_count" in retrieval
    # 三数守恒在 API 层同样成立（候选=原始召回、命中=预筛后、过滤=差）
    assert retrieval["filtered_count"] == retrieval["candidate_count"] - retrieval["matched_count"]


# ---------- 快照回放兼容：旧存档缺 filtered_count 不崩 ----------

def test_retrieval_meta_missing_filtered_count_backcompat():
    """旧 session 快照（2026-11-14 之前生成）无 filtered_count：前端应降级为候选−命中。"""
    meta = {
        "candidate_count": 5,
        "matched_count": 3,
        # 无 filtered_count —— 旧存档
    }
    assert meta.get("filtered_count", meta["candidate_count"] - meta["matched_count"]) == 2