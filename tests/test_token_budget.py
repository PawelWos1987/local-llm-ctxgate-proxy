"""Tests for the 2000-token hard cap in fetch_task_memory().

Strategy:
  - Import proxy.app as app (with env vars set so the module loads cleanly).
  - Replace app.count_tokens with a word-based mock (len(text.split())).
  - Use a FakePool that returns canned rows for the working-memory and
    memory queries.
  - Patch the helper functions so no real DB / network is needed.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

# --- Make the module importable and set env vars BEFORE import ---
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["CTXGATE_DB_DSN"] = "postgresql://localhost:5432/ctxproxy"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:29000/v1"
os.environ["CTXGATE_QWEN_TOKENIZER"] = " "  # space => skip tokenizer load

import proxy.app as app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine on a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call_fetch(session_id, messages, wm_budget=800, mem_budget=1200, total_budget=2000, wm_text="", crit_rows=(), rel_rows=(), terms=None):
    """Call app.fetch_task_memory with all dependencies patched."""
    async def _inner():
        return await app.fetch_task_memory(
            session_id, messages,
            wm_budget=wm_budget,
            mem_budget=mem_budget,
            total_budget=total_budget,
        )

    if terms is None:
        terms = set()

    with patch.object(app, "pool", _make_pool(wm_text, crit_rows, rel_rows)),          patch.object(app, "_resolve_task", new=AsyncMock(return_value="task-uuid-1")),          patch.object(app, "_context_blob", return_value=""),          patch.object(app, "_extract_terms", return_value=terms),          patch.object(app, "_already_in_context", return_value=False),          patch.object(app, "count_tokens", side_effect=lambda t: len(t.split())):
        return _run(_inner())


def _make_pool(wm_text="", crit_rows=(), rel_rows=()):
    """Build a FakePool with the given canned data."""
    class FakePool:
        async def fetchrow(self, query, *args):
            if "working_memory" in query:
                return {"content": wm_text} if wm_text else None
            return None

        async def fetch(self, query, *args):
            if "importance=10" in query:
                return list(crit_rows)
            if "ILIKE" in query:
                return list(rel_rows)
            return []

        async def execute(self, query, *args):
            return "0"

    return FakePool()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_normal_small_rows_fit_budget():
    """Small working memory + a few small memories fit well under 2000."""
    wm = "STATE: doing task X"           # 5 words
    crit = [{"id": 1, "key": "FACT", "value": "the answer is 42"}]  # 5 words
    rel  = [{"id": 2, "key": "NOTE", "value": "remember to test"}]  # 4 words

    result = _call_fetch("sess-1", [{"role": "user", "content": "hello"}],
                         wm_budget=800, mem_budget=1200, total_budget=2000, wm_text=wm, crit_rows=crit, rel_rows=rel, terms={"hello"})
    assert result != ""
    assert "WORKING MEMORY" in result
    assert "FACT: the answer is 42" in result
    assert "NOTE: remember to test" in result
    # Total token count (word-based) must be <= 2000
    total = len(result.split())
    assert total <= 2000, f"Exceeded budget: {total} tokens"


def test_adversarial_oversized_row_rejected():
    """A single 5000-word row must be skipped, not truncated."""
    # 5000 words in one row
    giant_value = " ".join(["word"] * 5000)
    crit = [{"id": 1, "key": "GIANT", "value": giant_value}]

    result = _call_fetch("sess-2", [{"role": "user", "content": "hello"}],
                         wm_budget=800, mem_budget=1200, total_budget=2000, crit_rows=crit)
    # The giant row should NOT appear (it exceeds the 1200-token durable budget)
    assert "word" not in result or result == ""
    # Result should be empty (no working memory, only the giant row)
    assert result == "", f"Expected empty result, got {len(result)} chars"


def test_many_rows_capped_at_budget():
    """13 rows of 198 words each must be capped at <= 2000 total."""
    # 198 words per row; 13 rows = 2574 words total (over budget)
    row_text = " ".join(["w"] * 198)
    crit = [{"id": i, "key": f"CRIT{i}", "value": row_text} for i in range(1, 6)]
    rel  = [{"id": 10 + i, "key": f"REL{i}", "value": row_text} for i in range(1, 9)]

    result = _call_fetch("sess-3", [{"role": "user", "content": "hello"}],
                         wm_budget=800, mem_budget=1200, total_budget=2000, crit_rows=crit, rel_rows=rel, terms={"w"})
    total = len(result.split())
    assert total <= 2000, f"Exceeded budget: {total} tokens"
    assert total > 0, "Expected some rows to be injected"


def test_working_memory_plus_memories_within_budget():
    """Working memory (800) + memories (1200) together stay under 2000."""
    # 700-word working memory
    wm = " ".join(["state"] * 700)
    # 1000-word memory
    mem = " ".join(["fact"] * 1000)
    crit = [{"id": 1, "key": "BIG", "value": mem}]

    result = _call_fetch("sess-4", [{"role": "user", "content": "hello"}],
                         wm_budget=800, mem_budget=1200, total_budget=2000, wm_text=wm, crit_rows=crit)
    total = len(result.split())
    assert total <= 2000, f"Exceeded budget: {total} tokens"
    assert total > 0


def test_exact_budget_boundary():
    """Rows that exactly fill the budget are accepted; one more word over is not."""
    # Working memory: exactly 800 words
    wm = " ".join(["a"] * 800)
    # One memory row of exactly 1200 words
    mem = " ".join(["b"] * 1200)
    crit = [{"id": 1, "key": "EXACT", "value": mem}]

    result = _call_fetch("sess-5", [{"role": "user", "content": "hello"}],
                         wm_budget=800, mem_budget=1200, total_budget=2000, wm_text=wm, crit_rows=crit)
    total = len(result.split())
    # 800 (wm) + "WORKING MEMORY:" (2 words) + 1200 (mem) + "EXACT:" (1 word) = 2003
    # The "WORKING MEMORY: " prefix adds 2 words, "EXACT: " adds 1 word
    # So total = 800 + 2 + 1200 + 1 = 2003 > 2000
    # The memory row should be rejected because total + t > 2000
    assert total <= 2000, f"Exceeded budget: {total} tokens"


def test_no_pool_returns_empty():
    """If pool is None, fetch_task_memory returns empty string immediately."""
    async def _inner():
        return await app.fetch_task_memory("sess-6", [{"role": "user", "content": "hi"}])

    with patch.object(app, "pool", None):
        result = _run(_inner())
    assert result == ""


def test_no_task_returns_empty():
    """If _resolve_task returns None, fetch_task_memory returns empty string."""
    async def _inner():
        return await app.fetch_task_memory("sess-7", [{"role": "user", "content": "hi"}])

    with patch.object(app, "pool", _make_pool()),          patch.object(app, "_resolve_task", new=AsyncMock(return_value=None)):
        result = _run(_inner())
    assert result == ""
