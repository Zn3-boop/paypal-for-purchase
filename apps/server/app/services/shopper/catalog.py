"""演示商品目录加载器。

从冻结的本地 fixture (data/catalog.json) 加载商品候选。零网络、零模型依赖，
保证：
  - 演示/录视频时无外部调用（外部搜索不可用也不影响）
  - 部署包不带 3.3GB 模型文件

商品条目结构（见 catalog.json）:
  id / name / category / price / currency / tags / description

加载结果带进程内缓存；返回深拷贝副本，防止调用方意外修改全局目录。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "catalog.json"


@lru_cache(maxsize=1)
def _load_catalog() -> list[dict[str, Any]]:
    if not _CATALOG_PATH.exists():
        logger.error("catalog.json 缺失: %s", _CATALOG_PATH)
        return []
    with _CATALOG_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    logger.info("加载演示商品目录 %d 件（fixture v%s）", len(items), data.get("version", "?"))
    return items


def get_catalog_items() -> list[dict[str, Any]]:
    """返回全部商品（深拷贝）。"""
    import copy

    return copy.deepcopy(_load_catalog())


def get_item(item_id: str) -> dict[str, Any] | None:
    for item in _load_catalog():
        if str(item.get("id")) == item_id:
            import copy

            return copy.deepcopy(item)
    return None