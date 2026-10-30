"""P2-10 收尾：llm_parse_with_retry 序列化修复护栏。

真实修复场景（LLM_MODE=real 时才走 beta.parse）：SDK 的
ParsedChatCompletionMessage[ContentType] 泛型模型直接 model_dump() 序列化
parsed 字段（Optional[~ResponseFormatT]）会触发 PydanticSerializationUnexpectedValue
警告（七个响应模型每个一条）。修复后排除 parsed 再手动转 dict。
mock 模式不覆盖此路径，故用 fake client 模拟 beta.parse 成功以锁定回归。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import asyncio
import types
import warnings
from typing import Any

import pytest
from pydantic_core import PydanticSerializationUnexpectedValue

from app.services.llm import client as llm_client
from app.services.llm.client import IntentDetectOutput, llm_parse_with_retry


class _FakeParse:
    """模拟 openai beta.chat.completions.parse 的成功返回。"""

    def __init__(self, parsed_model):
        self._parsed = parsed_model

    async def __call__(self, **kwargs):
        from openai.types.chat.parsed_chat_completion import (
            ParsedChatCompletion,
            ParsedChatCompletionMessage,
            ParsedChoice,
        )

        msg = ParsedChatCompletionMessage(role="assistant", content=None, parsed=self._parsed)
        choice = ParsedChoice(finish_reason="stop", index=0, message=msg)
        resp = ParsedChatCompletion(
            id="chatcmpl-fake",
            object="chat.completion",
            created=0,
            model=os.environ.get("LLM_MODEL", "qwen3.8-flash"),
            choices=[choice],
        )
        return resp


class _FakeCompletions:
    def __init__(self, parsed_model):
        self.parse = _FakeParse(parsed_model)


class _FakeBeta:
    def __init__(self, parsed_model):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(parsed_model))
        self.chat.completions.parse.model = None


class _FakeClient:
    def __init__(self, parsed_model: Any):
        self.beta = _FakeBeta(parsed_model)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_warnings():
    yield


def test_llm_parse_no_pydantic_parsed_warning(monkeypatch):
    """修复后：beta.parse 成功路径不产生 parsed 序列化警告，last_raw.parsed 是 dict。"""
    parsed = IntentDetectOutput(intent="music", intent_confidence=0.95)
    fake = _FakeClient(parsed)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: fake)
    monkeypatch.setenv("LLM_MODE", "real")  # 仅让 beta.parse 分支被选择（实际走 beta.parse）

    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        result, last_raw = _run(
            llm_parse_with_retry("帮我买一张爵士唱片", IntentDetectOutput)
        )

    bad = [str(x.message) for x in rec
           if isinstance(x.message, PydanticSerializationUnexpectedValue)
           or ("PydanticSerialization" in str(x.message) and "parsed" in str(x.message))]
    assert not bad, f"不要出现 parsed 序列化警告: {bad[:5]}"

    assert result is not None and result.intent == "music"
    assert isinstance(last_raw, dict)
    assert isinstance(last_raw.get("parsed"), dict), "last_raw.parsed 应为已转 dict 的 JSON-safe 值"
    assert last_raw["parsed"].get("intent") == "music"


def test_llm_parse_last_raw_keeps_parsed_field(monkeypatch):
    """信息不丢：即便排除 SDK parsed 序列化，last_raw 仍含 parsed 的 dict 形态。"""
    parsed = IntentDetectOutput(intent="film", intent_confidence=0.8)
    fake = _FakeClient(parsed)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: fake)
    monkeypatch.setenv("LLM_MODE", "real")

    _, last_raw = _run(llm_parse_with_retry("买一张科幻电影蓝光", IntentDetectOutput))
    assert last_raw.get("parsed", {}).get("intent") == "film"
    assert not isinstance(last_raw.get("parsed"), IntentDetectOutput), "json-safe 要求，不能塞模型实例"