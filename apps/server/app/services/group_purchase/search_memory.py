"""Channel3 检索记忆 —— 「搜过什么 / 选过什么」变成可复用的快捷入口（赞助商长期使用证据）。

设计取舍（诚实声明）：
  - 只记事实，不做画像：每条 = 时间 / 会话 / 用户 / 检索原句 / 模式；
  - JSONL 持久化（默认 ./data/search_memory/），进程重启不丢；
  - 读取时按「规范化原句」去重，保留最近一次时间并统计次数；
  - **绝不把历史检索词自动拼进本次 Channel3 查询**：记忆只作为「再搜一次」的
    快捷入口与「选过的方案」回显，避免把上次的口味强加到这次需求上（幻觉式个性化）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SEARCHES_FILE = "searches.jsonl"
_SELECTIONS_FILE = "selections.jsonl"
_lock = threading.Lock()


def get_memory_dir() -> Path:
    return Path(os.getenv("SEARCH_MEMORY_DIR", "./data/search_memory"))


def get_searches_file() -> Path:
    d = get_memory_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / _SEARCHES_FILE


def get_selections_file() -> Path:
    d = get_memory_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / _SELECTIONS_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_search(session_id: str, user_id: str, query: str, mode: str = "group") -> dict[str, Any] | None:
    """记录一次检索请求（intent 提交时调用）。返回落盘条目或 None。"""
    text = (query or "").strip()
    if not text:
        return None
    entry = {
        "ts": _now(),
        "session_id": session_id,
        "user_id": user_id or "demo-user",
        "query": text,
        "mode": mode or "group",
    }
    try:
        with _lock:
            with open(get_searches_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - 不影响主链路
        logger.warning("search memory write failed: %s", exc)
        return None
    return entry


def record_selection(
    session_id: str,
    user_id: str,
    plan_id: str,
    total: float,
    item_names: list[str],
    query: str = "",
) -> dict[str, Any] | None:
    """记录一次方案选择（select_plan 时调用）。"""
    entry = {
        "ts": _now(),
        "session_id": session_id,
        "user_id": user_id or "demo-user",
        "plan_id": plan_id,
        "total": round(float(total), 2),
        "item_count": len(item_names),
        "items": list(item_names)[:8],
        "query": (query or "").strip(),
    }
    try:
        with _lock:
            with open(get_selections_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover
        logger.warning("selection memory write failed: %s", exc)
        return None
    return entry


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows.append(row)
    except OSError:  # pragma: no cover
        return []
    return rows


def recent_searches(limit: int = 8) -> list[dict[str, Any]]:
    """按原句去重后的最近检索记录（时间倒序）。"""
    rows = _read_rows(get_searches_file())
    latest: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in rows:
        key = _norm(row.get("query", ""))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        prev = latest.get(key)
        if prev is None or str(row.get("ts", "")) >= str(prev.get("ts", "")):
            latest[key] = row
    items = []
    for row in latest.values():
        items.append({**row, "count": counts[_norm(row.get("query", ""))]})
    items.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return items[: max(1, limit)]


def recent_selections(limit: int = 5) -> list[dict[str, Any]]:
    """最近选过的方案（时间倒序，足够用户「用同一个检索词再看一次」）。"""
    rows = _read_rows(get_selections_file())
    rows.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return rows[: max(1, limit)]