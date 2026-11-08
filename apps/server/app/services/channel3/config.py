"""Channel3 SDK 配置；部署关键值走 app.core.config。"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings


@dataclass(frozen=True)
class Channel3Config:
    api_key: str
    base_url: str  # 留空则使用 SDK 默认生产端点 https://api.trychannel3.com
    timeout_seconds: float
    connect_timeout_seconds: float
    max_retries: int
    language: str = ""
    country: str = ""
    currency: str = ""

    @classmethod
    def from_env(cls) -> Channel3Config:
        # 每次读取当前 Settings，保证测试环境配置实时生效。
        settings = get_settings()
        return cls(
            api_key=settings.channel3_api_key,
            base_url=settings.channel3_base_url,
            timeout_seconds=settings.channel3_timeout_seconds,
            connect_timeout_seconds=settings.channel3_connect_timeout_seconds,
            max_retries=settings.channel3_max_retries,
            language=settings.channel3_language,
            country=settings.channel3_country,
            currency=settings.channel3_currency,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)