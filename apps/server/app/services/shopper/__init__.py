"""Shopper 服务层 —— 品味驱动 agentic 购物核心链路。

核心链路：
  1. LLM 理解意图 -> 候选商品(frozen fixture) -> 本地排序 -> Orders v2 结账
  2. Channel3 提供真实商品搜索；失败时使用本地 catalog
  3. 排序器零模型文件（ranker.py：规则 + 字符 n-gram）

模块：
  - catalog: 冻结演示数据（零网络）
  - ranker:  本地兜底排序器
"""
from app.services.shopper.catalog import get_catalog_items, get_item
from app.services.shopper.ranker import ngram_similarity, rank_candidates

__all__ = [
    "get_catalog_items",
    "get_item",
    "rank_candidates",
    "ngram_similarity",
]