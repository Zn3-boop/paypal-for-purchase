"""Channel3 商品搜索服务 —— 支付/选品链路的 L1 真实商品来源。

支付子图的 taste_select 使用 Channel3 SDK 做真实商品搜索，失败再降级到本地 catalog。
"""
from app.services.channel3.client import (
    CHANNEL3_DISABLED,
    CHANNEL3_EMPTY,
    CHANNEL3_ERROR,
    CHANNEL3_OK,
    Channel3Result,
    Channel3SearchClient,
    get_channel3_client,
    search_products_for_query,
    search_products_with_status,
)

__all__ = [
    "CHANNEL3_DISABLED",
    "CHANNEL3_EMPTY",
    "CHANNEL3_ERROR",
    "CHANNEL3_OK",
    "Channel3Result",
    "Channel3SearchClient",
    "get_channel3_client",
    "search_products_for_query",
    "search_products_with_status",
]