"""Centralized settings for deployment-critical configuration.

Why this file exists
--------------------
The server previously read every configuration value ad-hoc via ``os.getenv``
scattered across modules. That makes deployment (Railway / Render / Vercel)
error-prone: there is no single contract describing which environment
variables the service needs.

This module uses `pydantic-settings` to define a typed ``Settings`` model for
the values that matter at deploy time:

   * LLM API (chat / agent replies)
  * CORS (must lock down the deployed web origin)

Design constraints
------------------
1. Configuration is validated by tests across environments
   that mutate ``os.environ`` with ``patch.dict`` *at call time*. So this
   module must never memoize a single resolved ``Settings`` instance in a way
   that would freeze stale environment values. We expose a plain ``settings``
   getter that constructs a fresh ``Settings`` on demand; callers that want a
   one-shot value use ``settings()`` directly.

2. Deep / long-tail tuning knobs (RAG store dirs, model polling thresholds,
   LangGraph recursion limits, …) stay where they are as plain
   ``os.getenv(...)`` reads. Only deployment-critical values are centralized.

3. ``extra="ignore"`` keeps forward compatibility with the many variables in
   ``apps/server/.env`` that are not (yet) modelled here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/server/env/.env e.g. D:\ecv - 副本\apps\server\.env
_SERVER_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_FILE = _SERVER_DIR / ".env"


class Settings(BaseSettings):
    """Deployment-critical configuration read from the environment.

    Field names are intentionally aligned with the variable names
    used in ``apps/server/.env.example``. pydantic-settings resolves environment variables
    case-insensitively by default.
    """

    model_config = SettingsConfigDict(
        env_file=_DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- Channel3 (payment/shopping L1 real product search) ----------
    # 支付子图 taste_select 使用真实商品搜索替代本地目录猜排。
    # base_url 留空则使用 SDK 默认生产端点 https://api.trychannel3.com。
    channel3_api_key: str = ""
    channel3_base_url: str = ""
    channel3_timeout_seconds: float = 10.0
    channel3_max_retries: int = 2
    channel3_language: str = ""
    channel3_country: str = ""
    channel3_currency: str = ""

    # ---------- LLM (chat / agent replies) ----------
    llm_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "openai/gpt-4o-mini"
    llm_mode: str = "mock"  # mock = 规则近似意图/解释；real = 真实调用 OpenAI 兼容 LLM
    llm_timeout_seconds: float = 45.0

    # ---------- CORS ----------
    cors_allow_all: bool = False
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------- PayPal (Orders v2 checkout / Payouts) ----------
    paypal_mode: str = "sandbox"
    paypal_sandbox_client_id: str = ""
    paypal_sandbox_secret: str = ""
    paypal_sandbox_api: str = "https://api-m.sandbox.paypal.com"
    paypal_rest_api: str = ""  # 生产环境; 为空时由 mode 推导

    # ---------- Payment agent（授权边界支付子图）----------
    payment_mode: str = "mock"  # mock = 假订单全链路；real = 真实 PayPal sandbox 订单

    # ---------- Application ----------
    app_env: str = "development"


def get_settings(*, cache: bool = False) -> Settings:
    """Return a Settings instance.

    ``cache=False`` (default) always reads the freshest environment variables,
    which is required by tests that mutate ``os.environ`` between calls.
    ``cache=True`` freezes the first resolution (fine for app startup paths
    that run exactly once, e.g. CORS config in ``main.py``).
    """
    if cache:
        return _cached_settings()
    return Settings()


@lru_cache(maxsize=1)
def _cached_settings() -> Settings:
    return Settings()