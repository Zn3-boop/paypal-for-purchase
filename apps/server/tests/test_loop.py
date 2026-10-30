import os

os.environ["GRAPH_ENV"] = "dev"
os.environ["LLM_MODE"] = "mock"

import pytest

from app.services.graph.state import init_state


class TestLoopSmoke:
    def test_init_state(self):
        s = init_state(raw_query="test query", user_id="test-user")
        assert s is not None
        assert s.get("raw_query") == "test query"