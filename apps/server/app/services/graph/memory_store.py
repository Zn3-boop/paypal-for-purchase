from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from filelock import FileLock

logger = logging.getLogger(__name__)

EVIDENCE_LEVEL_A = "A"
EVIDENCE_LEVEL_B = "B"
EVIDENCE_LEVEL_C = "C"
EVIDENCE_LEVEL_D = "D"

SIGNAL_STATUS_OPEN = "open"
SIGNAL_STATUS_PROCESSING = "processing"
SIGNAL_STATUS_RESOLVED = "resolved"


class MemoryStore:
    """
    长期记忆 Store：跨会话持久记忆。
    namespace 按 ("user", user_id) 隔离。
    使用 filelock 保护并发读写，防止 revision 丢失。
    """

    def __init__(self, store_dir: str = ""):
        self.store_dir = store_dir or os.getenv(
            "GRAPH_MEMORY_STORE_DIR",
            "./memory_store",
        )
        os.makedirs(self.store_dir, exist_ok=True)
        self._lock_path = os.path.join(self.store_dir, ".memory_store.lock")
        self._lock = FileLock(self._lock_path, timeout=10)

    def _user_dir(self, user_id: str) -> str:
        d = os.path.join(self.store_dir, user_id)
        os.makedirs(d, exist_ok=True)
        return d

    def put(
        self,
        user_id: str,
        key: str,
        value: dict[str, Any],
        level: str = EVIDENCE_LEVEL_A,
        owner: str = "",
        expire_at: str | None = None,
    ) -> None:
        if level == EVIDENCE_LEVEL_D:
            logger.debug(f"D级推论禁止固化到长期记忆: key={key}")
            return
        if not owner and level in (EVIDENCE_LEVEL_A, EVIDENCE_LEVEL_B):
            logger.warning(f"无owner实体写入长期记忆: key={key}, level={level}, 标记腐烂")

        path = os.path.join(self._user_dir(user_id), f"{key}.json")
        page_revision: list[dict] = []

        with self._lock:
            if level in (EVIDENCE_LEVEL_A, EVIDENCE_LEVEL_B) and os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    page_revision.append({
                        "value": existing.get("value", {}),
                        "timestamp": existing.get("timestamp", 0),
                    })
                    old_revisions = existing.get("page_revision", [])
                    if old_revisions:
                        page_revision.extend(old_revisions)
                except Exception as e:
                    logger.warning(f"读取旧记忆revision失败 key={key} err={e}")

            entry = {
                "key": key,
                "value": value,
                "level": level,
                "owner": owner,
                "expire_at": expire_at,
                "page_revision": page_revision,
                "timestamp": time.time(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False, default=str, indent=2)

    def get(self, user_id: str, key: str) -> dict[str, Any] | None:
        path = os.path.join(self._user_dir(user_id), f"{key}.json")
        if not os.path.exists(path):
            return None
        with self._lock:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

    def list_keys(self, user_id: str) -> list[str]:
        d = self._user_dir(user_id)
        if not os.path.exists(d):
            return []
        with self._lock:
            return [
                os.path.splitext(f)[0]
                for f in os.listdir(d)
                if f.endswith(".json")
            ]

    def delete(self, user_id: str, key: str) -> bool:
        path = os.path.join(self._user_dir(user_id), f"{key}.json")
        if not os.path.exists(path):
            return False
        with self._lock:
            os.remove(path)
            return True

    def get_open_signals(self, user_id: str = "") -> list[dict[str, Any]]:
        if not self._signals:
            return []
        open_sigs = [s for s in self._signals.values() if s.get("status") == SIGNAL_STATUS_OPEN]
        if user_id:
            open_sigs = [s for s in open_sigs if s.get("user_id") == user_id]
        return open_sigs

    def put_evidence_a(
        self,
        user_id: str,
        raw_query: str,
        tool_result: dict[str, Any],
        trace_id: str,
        owner: str = "",
    ) -> None:
        ts = int(time.time() * 1000)
        self.put(
            user_id,
            key=f"evidence_A_{ts}",
            value={
                "raw_query": raw_query,
                "tool_result": tool_result,
                "trace_id": trace_id,
            },
            level=EVIDENCE_LEVEL_A,
            owner=owner,
        )

    def put_evidence_b(
        self,
        user_id: str,
        tool_name: str,
        tool_args_hash: str,
        tool_result: dict[str, Any],
        trace_id: str,
        owner: str = "",
    ) -> None:
        ts = int(time.time() * 1000)
        self.put(
            user_id,
            key=f"evidence_B_{ts}",
            value={
                "tool_name": tool_name,
                "tool_args_hash": tool_args_hash,
                "tool_result_ok": tool_result.get("ok", False),
                "trace_id": trace_id,
            },
            level=EVIDENCE_LEVEL_B,
            owner=owner,
        )

    def put_user_preference(
        self,
        user_id: str,
        app_name: str,
        launch_count: int = 1,
        owner: str = "",
    ) -> None:
        existing = self.get(user_id, f"pref_app_{app_name}")
        if existing:
            launch_count = existing["value"].get("launch_count", 0) + 1
        self.put(
            user_id,
            key=f"pref_app_{app_name}",
            value={"app_name": app_name, "launch_count": launch_count},
            level=EVIDENCE_LEVEL_C,
            owner=owner,
        )


_store_instance: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = MemoryStore()
    return _store_instance


class SelfGoverningMemoryStore:
    """
    自治理记忆仓库：信号持久化到磁盘json，进程重启不丢失。
    使用 filelock 保护信号文件并发读写。
    """

    def __init__(self, base_store: MemoryStore | None = None):
        self.store = base_store or get_memory_store()
        self.signal_dir = os.path.join(self.store.store_dir, "signals")
        os.makedirs(self.signal_dir, exist_ok=True)
        self.signal_file = os.path.join(self.signal_dir, "signals.json")
        self._signal_lock = FileLock(self.signal_file + ".lock", timeout=10)
        self._signals: dict[str, dict[str, Any]] = self._load_signals()

    def _load_signals(self) -> dict[str, dict[str, Any]]:
        if not os.path.exists(self.signal_file):
            return {}
        with self._signal_lock:
            try:
                with open(self.signal_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"加载信号文件失败，重置空集合 err={e}")
                return {}

    def _dump_signals(self):
        with self._signal_lock:
            with open(self.signal_file, "w", encoding="utf-8") as f:
                json.dump(self._signals, f, ensure_ascii=False, indent=2, default=str)

    def get_open_signals(self, user_id: str = "") -> list[dict[str, Any]]:
        """返回本自治理仓库信号池中的待处理（open）校正信号。"""
        if not self._signals:
            return []
        open_sigs = [s for s in self._signals.values() if s.get("status") == SIGNAL_STATUS_OPEN]
        if user_id:
            open_sigs = [s for s in open_sigs if s.get("user_id") == user_id]
        return open_sigs

    def put_memory(
        self,
        user_id: str,
        key: str,
        value: dict[str, Any],
        evidence_level: str = EVIDENCE_LEVEL_A,
        source: str = "",
        expire_at: str | None = None,
        owner: str = "",
    ) -> dict[str, Any]:
        if evidence_level == EVIDENCE_LEVEL_D:
            logger.debug(f"D级推论禁止固化到长期记忆: key={key}")
            return {"conflict": [], "expired": [], "namespace": ("user", user_id)}
        self.store.put(user_id, key, value, level=evidence_level, owner=owner, expire_at=expire_at)
        conflict_items = self._detect_conflict(user_id)
        expired_items = self._detect_expired(user_id)
        return {
            "conflict": conflict_items,
            "expired": expired_items,
            "namespace": ("user", user_id),
        }

    def _detect_conflict(self, user_id: str) -> list[dict[str, Any]]:
        keys = self.store.list_keys(user_id)
        conflicts = []
        group: dict[str, int] = {}
        for k in keys:
            base = k.split("_")[0] if "_" in k else k
            group[base] = group.get(base, 0) + 1
        for gk, count in group.items():
            if count >= 2:
                conflicts.append({"entity_key": gk, "count": count})
        return conflicts

    def _detect_expired(self, user_id: str) -> list[dict[str, Any]]:
        keys = self.store.list_keys(user_id)
        now_ts = time.time()
        expired = []
        for k in keys:
            entry = self.store.get(user_id, k)
            if entry and isinstance(entry, dict):
                exp = entry.get("expire_at")
                if not exp and isinstance(entry.get("value"), dict):
                    exp = entry["value"].get("expire_at")
                if exp:
                    try:
                        from datetime import datetime
                        exp_dt = datetime.fromisoformat(exp).timestamp()
                        if now_ts > exp_dt:
                            expired.append({"key": k, "expire_at": exp})
                    except (ValueError, TypeError):
                        pass
        return expired

    def generate_correction_signal(
        self,
        user_id: str,
        signal_type: str,
        related_keys: list[str],
        detail: str,
        entity_key: str = "",
        evidence_level: str = "",
        owner: str = "",
    ) -> str:
        import uuid as _uuid
        sig_id = f"sig_{_uuid.uuid4().hex[:12]}"
        sig = {
            "signal_id": sig_id,
            "signal_type": signal_type,
            "user_id": user_id,
            "related_keys": related_keys,
            "entity_key": entity_key,
            "evidence_level": evidence_level,
            "owner": owner,
            "detail": detail,
            "status": SIGNAL_STATUS_OPEN,
            "timestamp": time.time(),
        }
        self._signals[sig_id] = sig
        self._dump_signals()
        logger.info(f"CorrectionSignal generated: {sig_id} type={signal_type} status=open user={user_id}")
        return sig_id

    def update_signal_status(
        self,
        sig_id: str,
        status: str,
        owner: str = "",
        comment: str = "",
    ) -> bool:
        sig = self._signals.get(sig_id)
        if not sig:
            return False
        valid_transitions = {
            SIGNAL_STATUS_OPEN: [SIGNAL_STATUS_PROCESSING, SIGNAL_STATUS_RESOLVED],
            SIGNAL_STATUS_PROCESSING: [SIGNAL_STATUS_RESOLVED],
        }
        current = sig.get("status", "")
        if status not in valid_transitions.get(current, []):
            logger.warning(f"Invalid signal transition: {sig_id} {current} -> {status}")
            return False
        sig["status"] = status
        if owner:
            sig["owner"] = owner
        if comment:
            sig["comment"] = comment
        self._dump_signals()
        logger.info(f"CorrectionSignal updated: {sig_id} status={status}")
        return True

    def get_signal(self, sig_id: str) -> dict[str, Any] | None:
        return self._signals.get(sig_id)

    def list_signals(self, user_id: str = "") -> list[dict[str, Any]]:
        lst = list(self._signals.values())
        if user_id:
            return [s for s in lst if s.get("user_id") == user_id]
        return lst

    def full_scan(self, user_id: str = "") -> dict[str, Any]:
        scan_result: dict[str, Any] = {"conflict": [], "expired": [], "signal_generated": 0}
        if user_id:
            cf = self._detect_conflict(user_id)
            exp = self._detect_expired(user_id)
            scan_result["conflict"].extend(cf)
            scan_result["expired"].extend(exp)
            for c in cf:
                self.generate_correction_signal(
                    user_id, "conflict", [c["entity_key"]], f"检测到实体冲突 {c}",
                    entity_key=c["entity_key"],
                )
                scan_result["signal_generated"] += 1
            for e in exp:
                self.generate_correction_signal(
                    user_id, "expired", [e["key"]], f"检测到过期记录 {e}",
                    entity_key=e["key"],
                )
                scan_result["signal_generated"] += 1
        return scan_result


_governing_store_instance: SelfGoverningMemoryStore | None = None


def get_governing_store() -> SelfGoverningMemoryStore:
    global _governing_store_instance
    if _governing_store_instance is None:
        _governing_store_instance = SelfGoverningMemoryStore()
    return _governing_store_instance