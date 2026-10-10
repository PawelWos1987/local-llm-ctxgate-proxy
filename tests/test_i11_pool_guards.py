"""I11-I13: missing 'pool is None' guards.

_resolve_task, knowledge_create, knowledge_search, knowledge_stats, and
memory_inject all dereference the global 'pool' (pool.fetchrow/fetch/execute)
without checking it is None. When the DB is not ready (pool=None), these
raise AttributeError instead of degrading gracefully.

Fixes:
  - _resolve_task: if not pool: return None
  - knowledge_create/search/stats: if not pool: return 503
  - memory_inject: if task_uuid is None: return 503 (companion to _resolve_task guard)
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["CTXGATE_DB_DSN"] = "postgresql://localhost:5432/ctxproxy"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:29000/v1"
os.environ["CTXGATE_QWEN_TOKENIZER"] = " "

import proxy.app as app  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _MockRequest:
    def __init__(self, payload):
        self._payload = payload
        self.state = type("State", (), {"tenant_id": "test-tenant"})()
    async def json(self):
        return self._payload


def test_i11_resolve_task_none_pool_returns_none():
    """_resolve_task with pool=None must return None, not raise AttributeError."""
    with patch.object(app, "pool", None):
        result = _run(app._resolve_task("some-session", create=False))
    assert result is None, f"expected None, got {result!r}"


def test_i12_knowledge_endpoints_none_pool_return_503():
    """knowledge_create/search/stats with pool=None must return 503, not AttributeError."""
    with patch.object(app, "pool", None):
        # knowledge_create (POST /knowledge)
        req = _MockRequest({"domain": "d", "key": "k", "value": "v"})
        resp_create = _run(app.knowledge_create(req))
        assert resp_create.status_code == 503, f"create: expected 503, got {resp_create.status_code}"

        # knowledge_search (GET /knowledge/search)
        req_search = _MockRequest({})
        resp_search = _run(app.knowledge_search(req_search, q="test"))
        assert resp_search.status_code == 503, f"search: expected 503, got {resp_search.status_code}"

        # knowledge_stats (GET /knowledge/stats)
        req_stats = _MockRequest({})
        resp_stats = _run(app.knowledge_stats(req_stats))
        assert resp_stats.status_code == 503, f"stats: expected 503, got {resp_stats.status_code}"


def test_i13_memory_inject_none_pool_returns_503():
    """memory_inject (POST /memory/inject) with pool=None must return 503, not AttributeError.
    _resolve_task returns None when pool is None; memory_inject must handle that."""
    with patch.object(app, "pool", None):
        req = _MockRequest({"task_id": "t1", "content": "hello"})
        resp = _run(app.memory_inject(req))
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}"

