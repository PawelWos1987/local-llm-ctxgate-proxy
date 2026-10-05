"""Behavioral tests for the _summarize_trimmed_messages fixes (BUG 1-2).

These import the real proxy/app.py module, monkeypatch the asyncpg pool and the
_call_4b 4B-model call, and assert the summarizer actually works:
  (a) one slice  -> watermark advances, a root-summary row is stored,
      in_flight is False, summary_slices_failed == 0
  (b) second trigger with no new messages -> NO new phase row
  (c) forced exception in the root step -> in_flight is still False

No live DB and no live model are used; a fake in-memory pool records every
SQL statement so the tests can assert on what was (not) written.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import app  # noqa: E402  (module import is safe: pool stays None, no DB)


class _FakePool:
    """In-memory stand-in for the asyncpg pool used by the summarizer.

    Routes by the SQL text (the first positional argument). Records every
    execute() call so tests can assert on the writes that happened.
    """

    def __init__(self, task_id=None, max_phase=0):
        self.task_id = task_id
        self.max_phase = max_phase
        self.executes = []

    @staticmethod
    def _sql(args):
        return args[0] if args else ""

    async def execute(self, sql, *args):
        self.executes.append(sql)
        return "INSERT 0 1"

    async def fetchrow(self, sql, *args):
        s = self._sql((sql,) + args)
        if "MAX(phase_number)" in s:
            return {"mp": self.max_phase}
        return None  # no prior root summary

    async def fetch(self, sql, *args):
        s = self._sql((sql,) + args)
        if "phase_summaries" in s and "SELECT" in s:
            return [{"phase_number": 1,
                     "summary": "CURRENT TASK/PHASE: verify summarizer fixes."}]
        return []


class SummarizerFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Isolate module state so tests do not share globals.
        app.pool = None
        app.session_compactions = {}
        app._summary_task_locks = {}
        app._summary_last_attempt = {}
        app._trim_summary_last = {}
        app._slice_log = []
        app.metrics["summary_slices_ok"] = 0
        app.metrics["summary_slices_failed"] = 0

    def _seed(self, task_id, ws):
        app.session_compactions[ws["session_key"]] = ws
        app.pool = _FakePool(task_id=task_id, max_phase=ws.get("max_phase", 0))

    def _msgs(self, n):
        return [{"role": "user" if i % 2 else "assistant",
                 "content": "m%03d " % i + ("x" * 40)} for i in range(n)]

    # ---- (a) one slice fully succeeds --------------------------------------
    async def test_a_one_slice_advances_watermark_and_stores_root(self):
        task = "11111111-1111-4111-8111-111111111111"
        self._seed(task, {
            "session_key": "sk_a", "cut": 12, "cut_anchor": 0,
            "cut_prev_anchor": 0, "seed_sig": "seed_a",
            "summarized_through": 0, "dropped_total": 4, "in_flight": True,
        })
        calls = []
        root_text = ("CURRENT TASK/PHASE: verify the summarizer bug fixes are in "
                     "place. COMPLETED: patched BUG 1 through BUG 5 in "
                     "proxy/app.py with evidence from the unit tests. IN PROGRESS: "
                     "writing the behavioral test suite. NEXT STEP: run pytest and "
                     "confirm all green. DECISIONS/CONSTRAINTS: never touch port "
                     "9201, only kill own PIDs. DO NOT REDO: the five bug fixes.")

        async def fake_call_4b(*a, **k):
            calls.append(k)
            if k.get("json_mode"):
                return {"state_update": {"current_state": root_text}}
            return "CURRENT TASK/PHASE: phase one stored."

        with patch.object(app, "_call_4b", new=fake_call_4b):
            await app._summarize_trimmed_messages(task, "sk_a", self._msgs(12))
        ws = app.session_compactions["sk_a"]
        self.assertEqual(ws["summarized_through"], 4, "watermark must advance to dropped_total")
        self.assertTrue(any("INSERT INTO proxy.session_summaries" in e for e in app.pool.executes),
                        "a root summary row must be stored")
        self.assertFalse(ws["in_flight"], "in_flight must be released")
        self.assertEqual(app.metrics["summary_slices_failed"], 0)
        self.assertGreaterEqual(len(calls), 2, "expected at least phase + root 4B calls")

    # ---- (b) no new messages -> no new phase row ---------------------------
    async def test_b_no_new_messages_creates_no_phase_row(self):
        task = "22222222-2222-4222-8222-222222222222"
        self._seed(task, {
            "session_key": "sk_b", "cut": 4, "cut_anchor": 0,
            "cut_prev_anchor": 0, "seed_sig": "seed_b",
            "summarized_through": 4, "dropped_total": 4, "in_flight": True,
        })
        calls = []

        async def fake_call_4b(*a, **k):
            calls.append(k)
            return "x"

        with patch.object(app, "_call_4b", new=fake_call_4b):
            await app._summarize_trimmed_messages(task, "sk_b", self._msgs(4))
        ws = app.session_compactions["sk_b"]
        self.assertFalse(any("INSERT INTO proxy.phase_summaries" in e for e in app.pool.executes),
                         "no new phase row may be created when there is nothing new")
        self.assertFalse(ws["in_flight"])
        self.assertEqual(len(calls), 0, "no 4B calls when there is nothing to summarize")

    # ---- (c) forced root exception -> in_flight still released -------------
    async def test_c_root_exception_releases_in_flight(self):
        task = "33333333-3333-4333-8333-333333333333"
        self._seed(task, {
            "session_key": "sk_c", "cut": 8, "cut_anchor": 0,
            "cut_prev_anchor": 0, "seed_sig": "seed_c",
            "summarized_through": 0, "dropped_total": 3, "in_flight": True,
        })

        async def fake_call_4b(*a, **k):
            if k.get("json_mode"):
                raise RuntimeError("forced root failure")
            return "CURRENT TASK/PHASE: phase stored before the root step blew up."

        with patch.object(app, "_call_4b", new=fake_call_4b):
            await app._summarize_trimmed_messages(task, "sk_c", self._msgs(8))
        ws = app.session_compactions["sk_c"]
        self.assertFalse(ws["in_flight"], "in_flight must be False even after the root exception")
        self.assertEqual(ws["summarized_through"], 0, "watermark must NOT advance on failure")
        self.assertFalse(any("INSERT INTO proxy.session_summaries" in e for e in app.pool.executes),
                         "no root summary may be stored when the root step failed")
        self.assertEqual(app.metrics["summary_slices_failed"], 1)


if __name__ == "__main__":
    unittest.main()
