"""Channel3 区域/币种配置透传（P0-1，2026-11-14）。

此前 `CHANNEL3_LANGUAGE/COUNTRY/CURRENCY` 三个配置位从未传给 SDK，
实际一直跑在 SDK 默认 en/US/USD 上（写了不用）。本文件钉死两条契约：
  1. 配置非空时，`_search_sync` 必须把 language/country/currency 传给 SDK 构造；
  2. 配置为空时**不得**传空字符串（保留 SDK 的 country→currency/language 推断），
     否则“没配”会变成“配了空值”，覆盖掉 SDK 默认行为。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PAYMENT_MODE", "mock")

from app.services.channel3.client import Channel3SearchClient  # noqa: E402
from app.services.channel3.config import Channel3Config  # noqa: E402


def _client(language="", country="", currency="") -> Channel3SearchClient:
    return Channel3SearchClient(
        Channel3Config(
            api_key="test-key",
            base_url="",
            timeout_seconds=5.0,
            connect_timeout_seconds=3.0,
            max_retries=1,
            language=language,
            country=country,
            currency=currency,
        )
    )


def _fake_sdk(record: dict):
    class FakeProducts:
        def search(self, **kwargs):
            record.update(kwargs)
            return SimpleNamespace(items=[])

    return SimpleNamespace(products=FakeProducts())


def _capture_channel3(monkeypatch, captured: dict):
    """替换 channel3_sdk.Channel3 构造器：记录 kwargs，返回假 products.search。

    透传发生在 SDK **构造级**（Channel3(..., language=...)），不是 search 参数，
    所以必须走 `_get_sdk` 的真实构造路径来断言。
    """

    class FakeChannel3:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        @property
        def products(self):
            class P:
                def search(self, **kwargs):
                    return SimpleNamespace(items=[])

            return P()

    monkeypatch.setattr("channel3_sdk.Channel3", FakeChannel3)
    return FakeChannel3


def test_configured_locale_is_passed_to_sdk(monkeypatch):
    """配置了 language/country/currency → 构造 SDK 时必须透传（P0-1 核心）。"""
    captured: dict = {}
    _capture_channel3(monkeypatch, captured)
    client = _client(language="de", country="DE", currency="EUR")
    client._search_sync("query", 5, 0)
    assert captured["language"] == "de"
    assert captured["country"] == "DE"
    assert captured["currency"] == "EUR"


def test_unset_locale_is_omitted_not_empty_string(monkeypatch):
    """未配置 → 不传空串（否则覆盖 SDK 的 country→currency 默认推断）。"""
    captured: dict = {}
    _capture_channel3(monkeypatch, captured)
    client = _client()
    client._search_sync("query", 5, 0)
    assert "language" not in captured
    assert "country" not in captured
    assert "currency" not in captured