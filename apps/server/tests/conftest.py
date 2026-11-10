import os

os.environ.setdefault("GRAPH_ENV", "dev")
os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("TOOL_DEF_CONFIG_PATH", "config/tools.yaml")

import pytest
import pytest_asyncio
pytest_plugins = ["pytest_asyncio"]


@pytest.fixture(autouse=True)
def _isolate_graph_memory_store(tmp_path):
    """Give every test a fresh graph.memory_store directory.

    P0-3 persistence (payment trust profile) writes user-keyed JSON into
    MemoryStore. Without isolation, fixed user_ids (http-user-ok etc.) would
    accumulate trust across runs: 0.3 -> 0.5 -> 0.62 -> ... crossing the auto-
    approve threshold (0.6) and silently changing assertions like
    needs_approval is True. Pointing the module singleton at a per-test tmp_path
    makes the whole suite order- and rerun-independent.
    """
    from app.services.graph import memory_store as _ms

    _ms._store_instance = _ms.MemoryStore(store_dir=str(tmp_path / "memory_store"))
    yield


@pytest.fixture(autouse=True)
def _isolate_session_store(tmp_path, monkeypatch):
    """Give every test a fresh session JSON directory.

    Sessions now persist to `data/sessions/*.json`. Without isolation, sessions
    created by one test (or by a previous manual demo run in the repo) would be
    restored by `get_session()` in unrelated tests, turning "session not found"
    assertions flaky. `SESSION_STORE_DIR` is read on every call, so a per-test
    tmp_path fully isolates persistence.
    """
    monkeypatch.setenv("SESSION_STORE_DIR", str(tmp_path / "sessions"))
    yield


@pytest.fixture(autouse=True)
def _isolate_audit_store(tmp_path, monkeypatch):
    """Keep audit JSONL out of the repo `data/` dir during tests.

    `AuditLogger` is a process-wide singleton whose dir is read once, so both the
    env var and the singleton must be reset per test.
    """
    from app.services.group_purchase import audit_log as _al

    monkeypatch.setenv("AUDIT_LOG_STORE_DIR", str(tmp_path / "audit_logs"))
    _al._global_logger = None
    yield
    _al._global_logger = None


@pytest.fixture(autouse=True)
def _isolate_order_store(tmp_path, monkeypatch):
    """Give every test a fresh order JSONL store and an empty in-memory registry.

    `_orders` is a process-wide dict; without clearing it, an order left in
    `paying` by one test would be visible (and lazily expired) in the next one.
    `ORDER_STORE_DIR` is read on every write, so a per-test tmp_path keeps
    expiry-regression tests from appending to the repo's `data/orders`.
    """
    from app.services.group_purchase import order_lifecycle as _ol

    monkeypatch.setenv("ORDER_STORE_DIR", str(tmp_path / "orders"))
    _ol._orders.clear()
    yield
    _ol._orders.clear()