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