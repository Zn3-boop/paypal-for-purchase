"""Channel3 商品搜索客户端（带缓存 + 三态状态 + 信任链双回退）。

为什么要三态而不是"成功/失败"两态：
  看板上的「推荐依据」必须能区分 **检索成功但没匹配（empty）** 与
  **检索本身挂了（error）**。前者是商品库事实，后者是可恢复故障（"暂时不可用"），
  两者的诚实文案不同，审计与口播也不同。

为什么要信任链双回退而不是盲加 certifi：
  httpx 默认只信 certifi；Windows/企业网络里代理根证书只在系统证书存储里，
  于是出现"时好时坏"。正确顺序是 **系统信任库优先 → certifi 回退**，
  两个都保持 CERT_REQUIRED + check_hostname，绝不 verify=False。
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import ssl
from collections import OrderedDict
from dataclasses import dataclass, field
from time import time
from typing import Any, Sequence

from app.services.channel3.config import Channel3Config

logger = logging.getLogger(__name__)

_channel3_client: Channel3SearchClient | None = None
_SOURCE = "channel3"
_DEFAULT_CURRENCY = "USD"
_CHANNEL3_ID_PREFIX = "ch3_"

# 三态（进字段，不只进文案）：ok / empty / error / disabled
CHANNEL3_OK = "ok"
CHANNEL3_EMPTY = "empty"
CHANNEL3_ERROR = "error"
CHANNEL3_DISABLED = "disabled"

# 响应缓存：5 分钟内同一 query 秒回（录制稳定的真正来源，且走真实代码路径）
_CACHE: OrderedDict = OrderedDict()
# 30 分钟：demo/录制窗口里「设备调试 + 开场口播」很容易超过 5 分钟，
# 缓存太短会让 A/B 与推荐泳道在镜头前同时打空（demo 没有"数据太旧"的问题，只有"数据没了"）
_CACHE_TTL = 1800.0
_CACHE_MAX = 32

_DEFAULT_TOTAL_TIMEOUT = 5.0
_DEFAULT_CONNECT_TIMEOUT = 3.0


@dataclass
class Channel3Result:
    """一次检索的结果 + 状态（items 为空时 status 仍然有意义）。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    status: str = CHANNEL3_OK
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == CHANNEL3_OK


def _cache_key(query: str, limit: int) -> str:
    """归一化 key：大小写 / 多余空白 / 换行不影响命中。"""
    normalized = re.sub(r"\s+", " ", (query or "").strip().lower())
    return f"{normalized}|{limit}"


def _verify_context(context: ssl.SSLContext) -> ssl.SSLContext:
    """永远不放松校验：只换信任来源，不关 verify。"""
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _system_ssl_context() -> ssl.SSLContext:
    """系统信任库（Windows 证书存储含企业/代理根），并回退 OpenSSL 默认路径。"""
    return _verify_context(ssl.create_default_context())


def _certifi_ssl_context() -> ssl.SSLContext | None:
    """公共根证书（httpx 的默认信任来源）。"""
    try:
        import certifi
    except Exception:  # pragma: no cover - certifi 缺失时只用系统库
        return None
    return _verify_context(ssl.create_default_context(cafile=certifi.where()))


def ssl_context_chain() -> list[ssl.SSLContext]:
    """系统信任库优先，失败再回退 certifi；两者都校验。"""
    chain = [_system_ssl_context()]
    fallback = _certifi_ssl_context()
    if fallback is not None:
        chain.append(fallback)
    return chain


# ---------------------------------------------------------------------------
# 应用层 DNS pin（2026-11-10 加入）
#   演示机网关/ISP DNS 把 api.trychannel3.com 的 CNAME 目标
#   （d-aklj24vug8.execute-api.us-east-1.amazonaws.com）解析成 127.0.0.1，
#   且时好时坏（8.8.8.8 与 --resolve 均拿到真实 AWS IP）。不开管理员权限、
#   不改 hosts、不关 TLS 校验的解法：只在 connect 阶段把目标 IP 固定下来，
#   URL 仍是 api.trychannel3.com → SNI / Host / 证书校验全部照旧。
#   仅当 CHANNEL3_DNS_PIN 非空时生效；空列表即恢复系统解析。
# ---------------------------------------------------------------------------
_DNS_PIN_HOSTS = frozenset(
    {
        "api.trychannel3.com",
        "d-aklj24vug8.execute-api.us-east-1.amazonaws.com",
    }
)
_DNS_PIN_IPS: tuple[str, ...] = ()
_original_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(*args, **kwargs):  # noqa: ANN002,ANN003
    """socket.getaddrinfo 的 pin 版：命中 _DNS_PIN_HOSTS 且配了 pin → 用固定 IP 解析。"""
    host = kwargs["host"] if "host" in kwargs else (args[0] if args else None)
    if isinstance(host, str) and host in _DNS_PIN_HOSTS and _DNS_PIN_IPS:
        last_exc: OSError | None = None
        for ip in _DNS_PIN_IPS:
            try:
                if "host" in kwargs:
                    return _original_getaddrinfo(ip, *args, **{**kwargs, "host": ip})
                return _original_getaddrinfo(ip, *args[1:], **kwargs)
            except OSError as exc:  # 某个 pin IP 不可达就试下一个
                last_exc = exc
        if last_exc is not None:
            raise last_exc
    return _original_getaddrinfo(*args, **kwargs)


def _install_dns_pin(ips: Sequence[str]) -> None:
    """设置/清空 DNS pin；幂等，传空序列即恢复系统解析。"""
    global _DNS_PIN_IPS
    _DNS_PIN_IPS = tuple(dict.fromkeys(ips))
    socket.getaddrinfo = _pinned_getaddrinfo if _DNS_PIN_IPS else _original_getaddrinfo
    if _DNS_PIN_IPS:
        logger.info(
            "Channel3 DNS pin 生效：%s → %s",
            sorted(_DNS_PIN_HOSTS),
            list(_DNS_PIN_IPS),
        )


class Channel3SearchClient:
    def __init__(self, config: Channel3Config | None = None) -> None:
        self._config = config or Channel3Config.from_env()
        # 应用层 DNS pin：系统解析被污染时固定真实 IP（仅 pin 域名生效，TLS 校验不放松）
        _install_dns_pin(self._config.dns_pin)
        self._contexts: list[ssl.SSLContext] = ssl_context_chain()
        # 每个信任来源一个 SDK 实例（SDK 内部持有自己的 httpx.Client）
        self._sdks: list[Any | None] = [None] * len(self._contexts)

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def config(self) -> Channel3Config:
        return self._config

    @property
    def timeouts(self) -> tuple[float, float]:
        total = self._config.timeout_seconds or _DEFAULT_TOTAL_TIMEOUT
        connect = self._config.connect_timeout_seconds or _DEFAULT_CONNECT_TIMEOUT
        return float(total), float(connect)

    def _get_sdk(self, index: int) -> Any:
        if self._sdks[index] is None:
            import httpx
            from channel3_sdk import Channel3

            total, connect = self.timeouts
            http_client = httpx.Client(
                verify=self._contexts[index],
                timeout=httpx.Timeout(total, connect=connect),
            )
            kwargs: dict[str, Any] = {
                "api_key": self._config.api_key,
                "max_retries": self._config.max_retries,
                "httpx_client": http_client,
            }
            if self._config.base_url:
                kwargs["base_url"] = self._config.base_url
            self._sdks[index] = Channel3(**kwargs)
        return self._sdks[index]

    def _search_sync(self, query: str, limit: int, index: int) -> list[Any]:
        client = self._get_sdk(index)
        pager = client.products.search(query=query, limit=limit)
        return list(pager)

    async def search_products_with_status(self, query: str, limit: int = 8) -> Channel3Result:
        """检索并给出三态状态；SSL 校验失败时按信任链回退（仍保持校验）。"""
        if not self.enabled:
            logger.info("Channel3 未配置 key，跳过 L1 检索")
            return Channel3Result([], CHANNEL3_DISABLED, "未配置 CHANNEL3_API_KEY")
        query = (query or "").strip()
        if not query:
            return Channel3Result([], CHANNEL3_EMPTY, "检索词为空")

        cert_errors: list[str] = []
        for index in range(len(self._contexts)):
            try:
                products = await asyncio.to_thread(self._search_sync, query, limit, index)
                normalized = [n for n in (_normalize_product(p) for p in products) if n]
                if not normalized:
                    logger.info("Channel3 检索成功但 0 结果（trust#%d）：%s", index + 1, query)
                    return Channel3Result([], CHANNEL3_EMPTY, "检索成功，商品库无匹配")
                logger.info(
                    "Channel3 检索命中 %d 条（trust#%d/%d）",
                    len(normalized), index + 1, len(self._contexts),
                )
                return Channel3Result(normalized, CHANNEL3_OK, "")
            except ssl.SSLCertVerificationError as exc:
                # 只有"证书校验"才换信任来源重试；这是信任库差异，不是关校验。
                cert_errors.append(str(exc))
                logger.warning(
                    "Channel3 信任来源 #%d 校验失败，回退下一个：%s", index + 1, exc
                )
                continue
            except Exception as exc:
                logger.warning("Channel3 检索失败：%s", exc)
                return Channel3Result([], CHANNEL3_ERROR, f"请求失败：{type(exc).__name__}")

        logger.warning("Channel3 全部信任来源校验失败：%s", cert_errors)
        return Channel3Result([], CHANNEL3_ERROR, "证书校验失败（系统库与 certifi 均不通过）")

    async def search_products(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """兼容旧调用点：只要 items。"""
        result = await self.search_products_with_status(query, limit)
        return result.items


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


async def search_products_with_status(query: str, limit: int = 8) -> Channel3Result:
    """便捷入口：带 5 分钟 TTL 缓存 + 三态的检索。

    缓存只存 ok/empty（成功过的事实），**不缓存 error** ——
    否则一次网络抖动会被钉住 5 分钟，演示前"重试也救不回来"。
    """
    key = _cache_key(query, limit)
    now = time()
    entry = _CACHE.get(key)
    if entry is not None and now - entry["timestamp"] < _CACHE_TTL:
        _CACHE.move_to_end(key)
        logger.info("Channel3 缓存命中：%s", query)
        return Channel3Result(list(entry["items"]), entry["status"], entry["message"])

    result = await get_channel3_client().search_products_with_status(query, limit)
    if result.status != CHANNEL3_ERROR:
        _CACHE[key] = {
            "items": list(result.items),
            "status": result.status,
            "message": result.message,
            "timestamp": now,
        }
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return result


async def search_products_for_query(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """兼容旧调用点：只返回 items（状态请用 search_products_with_status）。"""
    result = await search_products_with_status(query, limit)
    return result.items