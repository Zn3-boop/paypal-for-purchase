"""Channel3 商品搜索客户端（带缓存版本）。"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from time import time
from typing import Any

from app.services.channel3.config import Channel3Config

logger = logging.getLogger(__name__)

_channel3_client: Channel3SearchClient | None = None
_SOURCE = "channel3"
_DEFAULT_CURRENCY = "USD"
_CHANNEL3_ID_PREFIX = "ch3_"

# 响应缓存
_CACHE: OrderedDict = OrderedDict()
_CACHE_TTL = 300.0  # 5 minutes


class Channel3SearchClient:
    def __init__(self, config: Channel3Config | None = None) -> None:
        self._config = config or Channel3Config.from_env()
        self._sdk: Any | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def config(self) -> Channel3Config:
        return self._config

    def _get_sdk(self) -> Any:
        if self._sdk is None:
            from channel3_sdk import Channel3
            kwargs: dict[str, Any] = {
                "api_key": self._config.api_key,
                "timeout": self._config.timeout_seconds,
                "max_retries": self._config.max_retries,
            }
            if self._config.base_url:
                kwargs["base_url"] = self._config.base_url
            self._sdk = Channel3(**kwargs)
        return self._sdk

    async def search_products(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if not self.enabled:
            logger.info("Channel3 未配置 key，跳过 L1 搜索")
            return []
        query = (query or "").strip()
        if not query:
            return []
        try:
            products = await asyncio.to_thread(self._search_sync, query, limit)
            normalized = [_normalize_product(p) for p in products]
            logger.info("Channel3 搜索命中 %d 条", len(normalized))
            return normalized
        except Exception as exc:
            logger.warning("Channel3 搜索失败：%s", exc)
            return []

    def _search_sync(self, query: str, limit: int) -> list[Any]:
        client = self._get_sdk()
        pager = client.products.search(query=query, limit=limit)
        return list(pager)


def _normalize_product(p: Any) -> dict[str, Any]:
    try:
        title = getattr(p, "title", "") or ""
        product_id = getattr(p, "id", "") or ""
        category_obj = getattr(p, "category", None)
        category = getattr(category_obj, "title", None) or getattr(category_obj, "slug", None) or ""
        offers = list(getattr(p, "offers", None) or [])
        price = 0.0
        currency = _DEFAULT_CURRENCY
        if offers:
            first = offers[0]
            price_obj = getattr(first, "price", None)
            if price_obj:
                price = float(getattr(price_obj, "price", 0.0) or 0.0)
                currency = str(getattr(price_obj, "currency", None) or _DEFAULT_CURRENCY)
        tags = list(getattr(p, "key_features", None) or [])
        for brand in list(getattr(p, "brands", None) or []):
            name = getattr(brand, "name", None)
            if name and str(name).strip() and str(name) not in tags:
                tags.append(str(name).strip())
        image_url = ""
        images = list(getattr(p, "images", None) or [])
        if images:
            image_url = str(getattr(images[0], "url", "") or "")
        return {
            "id": f"{_CHANNEL3_ID_PREFIX}{product_id}",
            "name": title,
            "category": category,
            "price": round(price, 2),
            "currency": currency,
            "tags": [str(t) for t in tags],
            "description": getattr(p, "description", "") or "",
            "image_url": image_url,
            "source": _SOURCE,
        }
    except Exception as exc:
        logger.warning("归一化失败：%s", exc)
        return {}


def get_channel3_client() -> Channel3SearchClient:
    global _channel3_client
    if _channel3_client is None:
        _channel3_client = Channel3SearchClient()
    return _channel3_client


async def search_products_for_query(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """便捷入口：带缓存的商品搜索。"""
    cache_key = f"{query}_{limit}"
    now = time()
    if cache_key in _CACHE:
        entry = _CACHE[cache_key]
        if now - entry["timestamp"] < _CACHE_TTL:
            _CACHE.move_to_end(cache_key)
            logger.info("Channel3 缓存命中：%s", query)
            return list(entry["result"])
    client = get_channel3_client()
    result = await client.search_products(query, limit)
    _CACHE[cache_key] = {"result": result, "timestamp": time()}
    while len(_CACHE) > 32:
        _CACHE.popitem(last=False)
    return result