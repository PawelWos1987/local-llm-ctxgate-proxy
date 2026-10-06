"""Step 2: knowledge extraction once per user turn, with a kill switch.

Behavior under test in _fire_and_forget_extract:
  - same newest-user-message anchor twice -> only ONE extraction
  - a new (different) user message anchor -> a second extraction
  - a short user message (< 40 chars after stripping <turn-context>) -> none
  - CTXGATE_KNOWLEDGE_EXTRACT="0" -> none (kill switch)

We count "extraction calls" by patching app.extract_knowledge (the function
that issues the LM call). store_knowledge and _sync_deliverable_summary are
no-ops. No live DB / live model.
"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import app  # noqa: E402


MSG_A = "Please investigate the deployment failure in the auth service on the production cluster"
MSG_B = "Now fix the database connection pooling issue that caused the timeout errors yesterday"
MSG_SHORT = "hi there"  # 8 chars, well under 40


class _ExtractorCounter:
    """Records how many times extract_knowledge was invoked."""

    def __init__(self):
        self.calls = []

    async def extract(self, sid, skey, msgs):
        self.calls.append(list(msgs))
        return []

    async def store(self, items, sid, skey):
        return 0

    async def sync(self, sid):
        return None


class TestExtractDedup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app._last_extract_anchor = {}
        app.metrics["extract_skipped_same_turn"] = 0
        app.metrics["extract_skipped_short"] = 0
        app._EXTRACT_IN_FLIGHT = 0
        self.counter = _ExtractorCounter()

    async def _drive(self, *invocations, env=None):
        """Run _fire_and_forget_extract for each invocation under patched deps."""
        async def _inner():
            with patch.object(app, "extract_knowledge", self.counter.extract), \
                 patch.object(app, "store_knowledge", self.counter.store), \
                 patch.object(app, "_sync_deliverable_summary", self.counter.sync):
                for sid, skey, msgs in invocations:
                    await app._fire_and_forget_extract(sid, skey, msgs)
        if env is None:
            await _inner()
        else:
            with patch.dict(os.environ, env):
                await _inner()

    async def test_same_anchor_twice_one_call(self):
        """Same newest user message anchor twice -> exactly one extraction."""
        msgs1 = [{"role": "user", "content": MSG_A}]
        msgs2 = [{"role": "user", "content": MSG_A}]
        await self._drive(("sid", "skey", msgs1), ("sid", "skey", msgs2))
        self.assertEqual(len(self.counter.calls), 1)
        self.assertEqual(app.metrics["extract_skipped_same_turn"], 1)

    async def test_new_anchor_second_call(self):
        """A different user message -> a second extraction."""
        msgs1 = [{"role": "user", "content": MSG_A}]
        msgs2 = [{"role": "user", "content": MSG_B}]
        await self._drive(("sid", "skey", msgs1), ("sid", "skey", msgs2))
        self.assertEqual(len(self.counter.calls), 2)
        self.assertEqual(app.metrics["extract_skipped_same_turn"], 0)

    async def test_short_message_none(self):
        """Short user message (< 40 chars) -> no extraction."""
        msgs = [{"role": "user", "content": MSG_SHORT}]
        await self._drive(("sid", "skey", msgs))
        self.assertEqual(len(self.counter.calls), 0)
        self.assertEqual(app.metrics["extract_skipped_short"], 1)

    async def test_short_with_turn_context_none(self):
        """Short real content inside a turn-context block -> no extraction."""
        msgs = [{"role": "user",
                 "content": "<turn-context>\n<current-time>2026-10-06</current-time>\n</turn-context>hi there"}]
        await self._drive(("sid", "skey", msgs))
        self.assertEqual(len(self.counter.calls), 0)
        self.assertEqual(app.metrics["extract_skipped_short"], 1)

    async def test_env0_disables(self):
        """CTXGATE_KNOWLEDGE_EXTRACT=0 -> no extraction even for a fresh long msg."""
        msgs = [{"role": "user", "content": MSG_A}]
        await self._drive(("sid", "skey", msgs), env={"CTXGATE_KNOWLEDGE_EXTRACT": "0"})
        self.assertEqual(len(self.counter.calls), 0)

    async def test_env1_default_enables(self):
        """Default (env unset) -> extraction happens for a fresh long msg."""
        env = {k: v for k, v in os.environ.items() if k != "CTXGATE_KNOWLEDGE_EXTRACT"}
        with patch.dict(os.environ, env, clear=True):
            await self._drive(("sid", "skey", [{"role": "user", "content": MSG_A}]))
        self.assertEqual(len(self.counter.calls), 1)

    async def test_evict_clears_anchor(self):
        """_evict_stale_sessions clears _last_extract_anchor for evicted sessions."""
        app._last_extract_anchor = {"stale_key": "abc123"}
        app.SESSION_LAST_ACTIVE = {"stale_key": time.time() - (app.SESSION_TTL_HOURS * 3600) - 100}
        async def noop_cleanup():
            return None
        with patch.object(app, "_window_cleanup_ttl", noop_cleanup):
            await app._evict_stale_sessions()
        self.assertNotIn("stale_key", app._last_extract_anchor)


if __name__ == "__main__":
    unittest.main()
