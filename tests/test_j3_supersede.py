"""J3: SUPERSEDE must be transactional (INSERT + UPDATE in one transaction).

Without a transaction, if the INSERT succeeds but the UPDATE fails (or the
process dies between them), the new memory row exists but the old one is never
marked superseded — leaving two active memories with the same key.

Fix: wrap the SUPERSEDE branch in an asyncpg transaction on a single
acquired connection so INSERT + UPDATE are atomic.
"""

import asyncio
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


class _MockTx:
    """Minimal asyncpg transaction mock."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class _MockConn:
    """Mock asyncpg connection that records SQL on the pool."""
    def __init__(self, pool_ref):
        self._pool = pool_ref
    async def execute(self, sql, *args):
        self._pool.executed.append(("conn.execute", sql, args))
        return "UPDATE 1"
    async def fetchval(self, sql, *args):
        self._pool.executed.append(("conn.fetchval", sql, args))
        return 42  # new_id
    async def fetchrow(self, sql, *args):
        self._pool.executed.append(("conn.fetchrow", sql, args))
        return {"id": 99, "value": "existing value"}
    def transaction(self):
        self._pool.tx_count += 1
        return _MockTx()


class _AcquireCtx:
    """Context manager returned by pool.acquire() (sync, like real asyncpg)."""
    def __init__(self, conn):
        self._conn = conn
    def __enter__(self):
        return self._conn
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _MockPool:
    """Mock asyncpg pool supporting both pool-level and connection-level methods."""
    def __init__(self):
        self.executed = []
        self.tx_count = 0
        self._conn = _MockConn(self)
    # --- pool-level (current code path) ---
    async def fetchrow(self, sql, *args):
        self.executed.append(("pool.fetchrow", sql, args))
        return {"id": 99, "value": "existing value"}
    async def fetchval(self, sql, *args):
        self.executed.append(("pool.fetchval", sql, args))
        return 42
    async def execute(self, sql, *args):
        self.executed.append(("pool.execute", sql, args))
        return "UPDATE 1"
    # --- connection-level (fixed code path) ---
    def acquire(self):
        return _AcquireCtx(self._conn)
    def transaction(self):
        self.tx_count += 1
        return _MockTx()


def test_j3_supersede_uses_transaction():
    """The SUPERSEDE branch must use a transaction (INSERT+UPDATE atomic)."""
    pool = _MockPool()
    actions = [{"action": "SUPERSEDE", "type": "FACT", "importance": "NORMAL",
               "title": "test_key", "content": "new content"}]
    with patch.object(app, "pool", pool):
        _run(app._store_memory_actions("task-1", actions, "event-1"))

    assert pool.tx_count == 1, (
        f"expected 1 transaction for SUPERSEDE, got {pool.tx_count}. "
        "INSERT and UPDATE must be in the same transaction."
    )
    sqls = [s[1] for s in pool.executed]
    assert any("INSERT INTO proxy.memories" in s for s in sqls), "INSERT not found"
    assert any("UPDATE proxy.memories SET active=false" in s for s in sqls), "UPDATE not found"


def test_j3_new_action_no_transaction():
    """NEW action does not need a transaction (single INSERT)."""
    pool = _MockPool()
    actions = [{"action": "NEW", "type": "FACT", "importance": "NORMAL",
               "title": "new_key", "content": "new content"}]
    with patch.object(app, "pool", pool):
        _run(app._store_memory_actions("task-1", actions, "event-1"))

    assert pool.tx_count == 0, f"NEW action should not use a transaction, got {pool.tx_count}"
    sqls = [s[1] for s in pool.executed]
    assert any("INSERT INTO proxy.memories" in s for s in sqls), "INSERT not found"

