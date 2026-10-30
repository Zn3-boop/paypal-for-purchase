import json
import logging
import os
import time

from typing import Any

import httpx as httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# .env 已由 app.core.config.Settings 统一加载（env_file 指向 apps/server/.env）；
# 这里不再自行 load_dotenv。os 仍保留，供 ProviderManager 等可选路径使用。

try:
    from app.services.llm.rate_limiter import rate_limiter
except ImportError:
    class _NoOpRateLimiter:
        def get_cached(self, *a, **kw):
            return None
        async def wait_turn(self):
            pass
        def set_cached(self, *a, **kw):
            pass
    rate_limiter = _NoOpRateLimiter()


class LLMProviderError(RuntimeError):
    """Raised when the configured LLM provider request fails."""


class LLMContextTooLongError(LLMProviderError):
    """Raised when the upstream provider rejects the request because the context is too long."""


class LLMRateLimitError(LLMProviderError):
    """Raised when the upstream provider rate limits the request."""


class LLMModelDisabledError(LLMProviderError):
    """Raised when the configured model is disabled or unavailable."""


class LLMUpstreamServerError(LLMProviderError):
    """Raised when the upstream provider returns a 5xx error."""


class LLMBadResponseError(LLMProviderError):
    """Raised when the upstream provider returns an unexpected payload."""


def _classify_provider_error(status_code: int, response_text: str) -> LLMProviderError:
    normalized = (response_text or "").lower()
    message = f"LLM provider request failed: {status_code} {response_text}"

    if status_code in {400, 413, 414} and any(
        token in normalized
        for token in {"context", "token", "too long", "maximum context", "prompt is too long", "invalid_request"}
    ):
        return LLMContextTooLongError(message)
    if status_code in {401, 403, 404} and any(
        token in normalized
        for token in {"disabled", "unavailable", "not found", "no such model", "model"}
    ):
        return LLMModelDisabledError(message)
    if status_code == 429:
        return LLMRateLimitError(message)
    if status_code >= 500:
        return LLMUpstreamServerError(message)
    return LLMProviderError(message)


class LLMProvider:
    def __init__(self) -> None:
        # 部署关键 LLM 配置统一由 app.core.config.Settings 读取（env_file 加载 .env）。
        # 每次构造都重新解析，避免缓存导致测试中 patch.dict("os.environ", ...) 不生效。
        settings = get_settings()
        # 优先从 ProviderManager 读取动态配置
        self._provider = self._load_active_provider()
        if self._provider:
            self.api_key = self._provider.api_key
            self.base_url = self._provider.api_url.rstrip("/")
            self.model = self._provider.model
            self.timeout = settings.llm_timeout_seconds
            self.enabled = True
        else:
            # 降级到 .env 配置
            self.api_key = settings.llm_api_key.strip()
            self.base_url = settings.llm_base_url.rstrip("/")
            self.model = settings.llm_model
            self.timeout = settings.llm_timeout_seconds
            self.enabled = bool(self.api_key)

    def _load_active_provider(self):
        """从 ProviderManager 获取第一个启用的 provider"""
        try:
            from app.services.provider_manager import get_provider_manager
            pm = get_provider_manager()
            for p in pm.providers.values():
                if not p.enabled:
                    continue
                if not p.model:
                    continue
                if p.api_key:
                    return p
                if self._is_local_url(p.api_url):
                    return p
        except Exception:
            pass
        return None

    @staticmethod
    def _is_local_url(api_url: str) -> bool:
        try:
            from urllib.parse import urlparse
            host = urlparse(str(api_url or "")).hostname or ""
            return host in ("localhost", "127.0.0.1", "::1")
        except Exception:
            return False

    async def _request(
        self,
        payload: dict[str, Any],
        *,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """底层请求方法，返回完整 response JSON"""
        if not self.enabled:
            raise LLMProviderError("LLM provider is not configured. Please set LLM_API_KEY.")

        # 缓存 key 基于 payload 的 messages + model + temperature（不含 tools，因为工具调用不缓存）
        cache_key_payload = {
            "model": payload.get("model"),
            "messages": payload.get("messages"),
            "temperature": payload.get("temperature"),
            "response_format": payload.get("response_format"),
        } if use_cache else None

        if use_cache and cache_key_payload:
            cached = rate_limiter.get_cached(
                system_prompt="",  # 兼容旧缓存接口，实际用完整 payload
                user_prompt=json.dumps(cache_key_payload, ensure_ascii=False),
                temperature=payload.get("temperature", 0.2),
                response_format=payload.get("response_format"),
            )
            if cached is not None:
                # 解析缓存的 JSON
                try:
                    return json.loads(cached)
                except json.JSONDecodeError:
                    pass

        await rate_limiter.wait_turn()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        app_name = os.getenv("LLM_APP_NAME", "AI Desktop Agent")
        referer = os.getenv("LLM_HTTP_REFERER", "http://localhost")
        if referer:
            headers["HTTP-Referer"] = referer
        if app_name:
            headers["X-Title"] = app_name

        # 兼容不同平台的 URL 格式：检查是否已包含 chat/completions 或其他路径
        url = self.base_url
        # 检查URL是否已经包含了completion相关路径
        # 注意：/v4 和 /v1 结尾的路径仍需要拼接 /chat/completions
        has_completion_path = any(
            url.endswith(suffix) 
            for suffix in ["/chat/completions", "/completions"]
        )
        if not has_completion_path:
            # 对于 /v4、/v1 等基础路径，需要拼接 /chat/completions
            url = f"{url}/chat/completions"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            t0 = time.monotonic()
            response = await client.post(
                url,
                headers=headers,
                json=payload,
            )
            total_latency_ms = (time.monotonic() - t0) * 1000

        if response.status_code >= 400:
            self._record_llm_metrics(payload, total_latency_ms, error=True)
            raise _classify_provider_error(response.status_code, response.text)

        data = response.json()
        self._record_llm_metrics(payload, total_latency_ms, data=data)
        if use_cache and cache_key_payload:
            rate_limiter.set_cached(
                json.dumps(data, ensure_ascii=False),
                system_prompt="",
                user_prompt=json.dumps(cache_key_payload, ensure_ascii=False),
                temperature=payload.get("temperature", 0.2),
                response_format=payload.get("response_format"),
            )
        return data

    async def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
        use_cache: bool = True,
        task_type: str = "",
    ) -> str:
        """普通对话，返回文本内容。task_type 用于多模型路由（simple/medium/complex）"""
        model = self.model
        if task_type:
            try:
                from app.services.llm.model_router import get_model_router
                routing = get_model_router().route(task_type=task_type, query=user_prompt)
                model = routing["model"]
            except Exception:
                pass

        payload: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if response_format:
            payload["response_format"] = response_format

        data = await self._request(payload, use_cache=use_cache)
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMBadResponseError(f"Unexpected LLM response: {json.dumps(data, ensure_ascii=False)}") from exc

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """支持 function calling 的对话，返回完整 message dict（含 tool_calls）"""
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }

        data = await self._request(payload, use_cache=False)
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMBadResponseError(f"Unexpected LLM response: {json.dumps(data, ensure_ascii=False)}") from exc

    def _record_llm_metrics(
        self,
        payload: dict[str, Any],
        total_latency_ms: float,
        data: dict[str, Any] | None = None,
        error: bool = False,
    ) -> None:
        try:
            from app.services.observability.metrics_collector import (
                get_metrics_collector,
                LLMCallMetrics,
            )
            collector = get_metrics_collector()

            prompt_tokens = 0
            completion_tokens = 0
            ttft_ms = 0.0
            tpot_ms = 0.0

            if data and not error:
                usage = data.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)

                if completion_tokens > 0 and total_latency_ms > 0:
                    ttft_ms = total_latency_ms * 0.3
                    decode_time = total_latency_ms * 0.7
                    tpot_ms = decode_time / completion_tokens if completion_tokens > 0 else 0.0

            metrics = LLMCallMetrics(
                ttft_ms=ttft_ms,
                tpot_ms=tpot_ms,
                total_latency_ms=total_latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                model=payload.get("model", ""),
                is_retry=False,
                error_type="http_error" if error else "",
            )
            collector.record_llm_call(metrics)
        except Exception as e:
            logger.debug(f"Metrics recording failed (non-fatal): {e}")


provider = LLMProvider()