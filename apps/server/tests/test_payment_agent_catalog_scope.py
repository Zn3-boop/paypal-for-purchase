"""支付 Agent 的「购物语义 + 目录边界」契约。

事故背景（2026-11-05 录前联调，用户实测）：
    输入「帮我找一副 300 以内的降噪耳机」→ 回复只有四个字「处理完成。」，
    既没有推荐，也没有任何支付入口。两处根因：

      1. `_PAYMENT_KEYWORDS` 只认「买/下单/付款」，"找"不在其中 →
         `_has_payment_intent` 为 False → 支付子图整段没跑，state 全空，
         `_state_to_response` 落到空兜底「处理完成。」。
      2. 商品目录（app/services/shopper/data/catalog.json）只有 8 件文化/餐饮品，
         本来就没有耳机；就算进子图，ranker 也会拿「黑胶唱片」冒充耳机。

本文件锁两条契约：
    A. 找/推荐 + 购物信号（预算/品类）必须进支付子图；纯「找代码」不许被劫持。
    B. 目录外品类必须诚实收口：不推荐不相干商品、不出现「处理完成。」，
       并且给出目录真实可选 + 拼单入口。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.graph.graph import _has_payment_intent, _looks_like_shopping_request


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _agent(client, message: str, *, user_id: str, session_id: str):
    return client.post(
        "/api/v1/payment/agent",
        json={
            "message": message,
            "session_id": session_id,
            "user_id": user_id,
            "monthly_spent": 0.0,
        },
    )


def test_find_verb_with_budget_is_shopping_intent():
    """「找…以内」算购物请求；「帮我找一下代码」不算（不能被支付子图劫持）。"""
    assert _looks_like_shopping_request("帮我找一副 300 以内的降噪耳机") is True
    assert _looks_like_shopping_request("推荐个 200 以内的礼物") is True
    assert _looks_like_shopping_request("帮我找一下代码在哪") is False
    assert _looks_like_shopping_request("推荐一首歌听听") is False

    assert _has_payment_intent({"raw_query": "帮我找一副 300 以内的降噪耳机"}) is True
    assert _has_payment_intent({"raw_query": "帮我找一下代码在哪"}) is False
    assert _has_payment_intent({"raw_query": "你好，介绍一下你自己"}) is False


def test_find_verb_reaches_approval_gate_for_catalog_item(client):
    """目录内商品：找动词也要走到审批闸门（有推荐 + 待审批卡，不是空回复）。"""
    tag = uuid.uuid4().hex[:8]
    r = _agent(
        client,
        "帮我找一张周五晚上听的爵士唱片",
        user_id=f"catalog-find-{tag}",
        session_id=f"catalog-find-s-{tag}",
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["reply"] and body["reply"] != "处理完成。"
    assert body["needs_approval"] is True
    item = body["payment"]["item"]
    assert item["name"], f"应给出候选商品，实际 {item}"
    assert item["price"] and float(item["price"]) > 0


def test_off_catalog_item_returns_honest_no_match(client):
    """目录外品类（耳机）：不推荐不相干商品，也不要「处理完成。」。"""
    tag = uuid.uuid4().hex[:8]
    r = _agent(
        client,
        "帮我找一副 300 以内的降噪耳机",
        user_id=f"catalog-off-{tag}",
        session_id=f"catalog-off-s-{tag}",
    )
    assert r.status_code == 200, r.text
    body = r.json()

    reply = body["reply"]
    assert reply != "处理完成。"
    assert "没有找到" in reply
    assert "方案对比" in reply
    # 目录里真实存在的候选要被列出来，用户才知道能买什么
    assert "当前小铺里可选的有" in reply

    assert body["needs_approval"] is False
    assert not body["payment"]["item"]["name"]
