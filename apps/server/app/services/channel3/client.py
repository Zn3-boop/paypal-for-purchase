"""Channel3 商品搜索客户端 —— 懒创建、可降级、归一化为 catalog 结构。

设计要点：
  - 懒创建单例，无网络连接预热；请求超时/失败绝不硬失败，返回空列表并记日志。
  - products.search() 是同步 SDK（SyncPager），在 async 节点中用 asyncio.to_thread
    包裹，避免阻塞事件循环。
  - 归一化 Product -> catalog 条目 {id,name,category,price,currency,tags,description,
    image_url,product_url,source}，使下游 rank_candidates / PayPal 下单无需感知厂商差异。
  - 归一化后统一携带 source="channel3"，供可解释性面板透出真实商品来源。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.channel3.config import Channel3Config

logger = logging.getLogger(__name__)

_channel3_client: Channel3SearchClient | None = None

# 归一化后携带在条目上的来源标记；价格/品类缺失时的安全默认。
_SOURCE = "channel3"
_DEFAULT_CURRENCY = "USD"
_CHANNEL3_ID_PREFIX = "ch3_"


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
        """懒创建 SDK 客户端（同步 httpx 客户端，无连接预热）。"""
        if self._sdk is None:
            from channel3_sdk import Channel3

            kwargs: dict[str, Any] = {
                "api_key": self._config.api_key,
                "timeout": self._config.timeout_seconds,
                "max_retries": self._config.max_retries,
            }
            if self._config.base_url:
                kwargs["base_url"] = self._config.base_url
            if self._config.language:
                kwargs["language"] = self._config.language
            if self._config.country:
                kwargs["country"] = self._config.country
            if self._config.currency:
                kwargs["currency"] = self._config.currency
            self._sdk = Channel3(**kwargs)
            logger.info(
                "Channel3 SDK 已创建（timeout=%.1fs, retries=%d）",
                self._config.timeout_seconds,
                self._config.max_retries,
            )
        return self._sdk

    async def search_products(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """按 query 搜索真实商品，归一化为 catalog 结构；失败/空 key 返回 []。"""
        if not self.enabled:
            logger.info("Channel3 未配置 key（CHANNEL3_API_KEY 为空），跳过 L1 搜索")
            return []
        query = (query or "").strip()
        if not query:
            logger.warning("Channel3 搜索 query 为空，跳过")
            return []
        try:
            products = await asyncio.to_thread(self._search_sync, query, limit)
            normalized = [_normalize_product(p) for p in products]
            logger.info(
                "Channel3 搜索命中 %d 条（query=%r, limit=%d）",
                len(normalized),
                query,
                limit,
            )
            return normalized
        except Exception as exc:  # pragma: no cover - 降级不硬失败
            logger.warning(
                "Channel3 搜索失败，降级 L2 本地 catalog（query=%r, err=%s）",
                query,
                exc,
            )
            return []

    def _search_sync(self, query: str, limit: int) -> list[Any]:
        """同步执行 SDK 搜索并取出第一页 Product 列表。"""
        client = self._get_sdk()
        pager = client.products.search(query=query, limit=limit)
        items = list(pager)
        products = list(items)
        if not products and pager.response is not None:
            # 极端情况下 pager 迭代为空，但 response 里有 products 字段则直接取
            resp = pager.response
            if hasattr(resp, "products"):
                products = list(resp.products or [])
        return products
def _normalize_product(p: Any) -> dict[str, Any]:
    """Product(pydantic) -> catalog 条目 dict。"""
    try:
        title = getattr(p, "title", "") or ""
        product_id = getattr(p, "id", "") or ""
        description = getattr(p, "description", "") or ""

        # 品类：优先 category.title，退回 slug
        category_obj = getattr(p, "category", None)
        category = ""
        if category_obj is not None:
            category = getattr(category_obj, "title", None) or getattr(category_obj, "slug", None) or ""

        # 价格 / 货币：offers[0].price（Price{price, compare_at_price, currency}）
        offers = list(getattr(p, "offers", None) or [])
        price = 0.0
        currency = _DEFAULT_CURRENCY
        product_url = ""
        if offers:
            first = offers[0]
            price_obj = getattr(first, "price", None)
            if price_obj is not None:
                price = float(getattr(price_obj, "price", 0.0) or 0.0)
                currency = str(getattr(price_obj, "currency", None) or _DEFAULT_CURRENCY)
            product_url = str(getattr(first, "url", "") or "")

        # tags：key_features + 品牌名 + 品类名（供本地 ranker 的 n-gram 匹配）
        tags: list[str] = []
        for feat in list(getattr(p, "key_features", None) or []):
            if str(feat).strip():
                tags.append(str(feat).strip())
        for brand in list(getattr(p, "brands", None) or []):
            name = getattr(brand, "name", None)
            if name and str(name).strip() and str(name) not in tags:
                tags.append(str(name).strip())
        if category and category not in tags:
            tags.append(category)

        # 主图
        image_url = ""
        images = list(getattr(p, "images", None) or [])
        if images:
            first_img = images[0]
            image_url = str(getattr(first_img, "url", "") or "") or str(getattr(first_img, "cleaned_url", "") or "")

        return {
            "id": f"{_CHANNEL3_ID_PREFIX}{product_id}",
            "name": title,
            "category": category,
            "price": round(price, 2),
            "currency": currency,
            "tags": tags,
            "description": description,
            "image_url": image_url,
            "product_url": product_url,
            "source": _SOURCE,
            "raw_id": product_id,
        }
    except Exception as exc:  # pragma: no cover - 单条归一化失败不影响整体
        logger.warning("Channel3 Product 归一化失败，丢弃该条: %s", exc)
        return {}


def get_channel3_client() -> Channel3SearchClient:
    global _channel3_client
    if _channel3_client is None:
        _channel3_client = Channel3SearchClient()
    return _channel3_client


async def search_products_for_query(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """便捷入口：支付子图 taste_select 直接调用。"""
    client = get_channel3_client()
    return await client.search_products(query, limit)