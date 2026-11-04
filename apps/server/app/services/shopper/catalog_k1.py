"""K1 Group Purchase Catalog Loader

加载 K1 商品池 (装饰/奖品/饮用水/文具) 用于团购场景。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
import copy

_PATH = Path(__file__).resolve().parent / "data" / "k1_catalog.json"

@lru_cache(maxsize=1)
def _load_raw() -> list[dict[str, Any]]:
    if not _PATH.exists():
        return []
    with _PATH.open(encoding="utf-8") as f:
        d = json.load(f)
    return d.get("catalog", [])

def _to_standard_item(raw: dict[str, Any]) -> dict[str, Any]:
    tags = []
    if raw.get("eco_label"):
        tags.append("环保")
    if raw.get("material"):
        tags.append(raw["material"])
    if "一次性" in (raw.get("name") or ""):
        tags.append("一次性")
        tags.append("禁忌")
    return {
        "id": raw["id"],
        "name": raw["name"],
        "category": raw["category"],
        "price": float(raw.get("price", 0)),
        "currency": "USD",
        "tags": list(dict.fromkeys(tags)),
        "description": f"{raw['name']} | {raw.get('material','')} | 价格 {raw['price']} USD | {'环保' if raw.get('eco_label') else ''}".strip(" | "),
    }

def get_k1_items() -> list[dict[str, Any]]:
    raw_items = _load_raw()
    items = [_to_standard_item(r) for r in raw_items]
    return copy.deepcopy(items)

def get_k1_item(item_id: str) -> dict[str, Any] | None:
    for it in get_k1_items():
        if it["id"] == item_id:
            return it
    return None