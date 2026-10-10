"""Step 3: Role-aware tool-output capping in _summarize_trimmed_messages.

Verifies that:
  1. A 20k-char tool body is capped to ~1000 chars in the chunk sent to Mistral
  2. User text is preserved verbatim
  3. Assistant tool_call arguments are capped at 300 chars each
  4. Watermark advances and phase storage behave exactly as before
  5. CTXGATE_SUMMARY_TOOL_CAP_CHARS=0 disables the cap
  6. The real messages are NOT mutated
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import app  # noqa: E402


class _FakePool:
    def __init__(self, task_id=None, max_phase=0):
        self.task_id = task_id
        self.max_phase = max_phase
        self.executes = []

    @staticmethod
    def _sql(args):
        return args[0] if args else ""

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "INSERT 0 1"

    async def fetchrow(self, sql, *args):
        s = self._sql((sql,) + args)
        if "MAX(phase_number)" in s:
            return {"mp": self.max_phase}
        return None

    async def fetch(self, sql, *args):
        s = self._sql((sql,) + args)
        if "phase_summaries" in s and "SELECT" in s:
            return [{"phase_number": 1, "summary": "Phase 1: tool output summarized."}]
        return []


class SummaryToolCapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.pool = None
        app.session_compactions = {}
        app._summary_task_locks = {}
        app._summary_last_attempt = {}
        app._trim_summary_last = {}
        app.metrics["summary_slices_ok"] = 0
        app.metrics["summary_slices_failed"] = 0

    def _seed(self, task_id, ws):
        app.session_compactions[ws["session_key"]] = ws
        app.pool = _FakePool(task_id=task_id, max_phase=ws.get("max_phase", 0))

    # ---- (1) 20k tool body is capped, user text intact -----------------------
    async def test_tool_body_capped_user_intact(self):
        task = "aaaa0000-0000-4000-8000-000000000001"
        self._seed(task, {
            "session_key": "sk_cap1", "cut": 10, "cut_anchor": 0,
            "cut_prev_anchor": 0, "seed_sig": "s",
            "summarized_through": 0, "dropped_total": 3, "in_flight": True,
        })
        big_tool = "T" * 20000
        user_msg = "Please analyze the deployment logs and report back."
        msgs = [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": "Let me check the logs."},
            {"role": "tool", "content": big_tool},
        ]
        # Save originals to verify non-mutation
        orig_tool = msgs[2]["content"]
        orig_user = msgs[0]["content"]

        chunks_seen = []
        async def fake_call_4b(*a, **k):
            if k.get("json_mode"):
                return {"state_update": {"current_state":
                    "CURRENT TASK/PHASE: analyzed logs. COMPLETED: read tool output. "
                    "IN PROGRESS: none. NEXT STEP: report. DECISIONS/CONSTRAINTS: none. "
                    "DO NOT REDO: log analysis."}}
            # Capture the chunk text
            content = a[0][0]["content"] if a and a[0] else ""
            chunks_seen.append(content)
            return "CURRENT TASK/PHASE: phase stored."

        with patch.object(app, "_call_4b", new=fake_call_4b):
            await app._summarize_trimmed_messages(task, "sk_cap1", msgs)

        # Messages must NOT be mutated
        self.assertEqual(msgs[2]["content"], orig_tool, "tool message was mutated")
        self.assertEqual(msgs[0]["content"], orig_user, "user message was mutated")

        # At least one phase call must have been made
        self.assertGreater(len(chunks_seen), 0, "no phase chunks captured")
        all_text = "\n".join(chunks_seen)

        # The 20k tool body must be capped (well under 20000)
        self.assertNotIn(big_tool, all_text, "full 20k tool body leaked into chunk")
        # User text must be intact
        self.assertIn(user_msg, all_text, "user text missing from chunk")
        # The omission marker should be present
        self.assertIn("chars omitted", all_text, "omission marker missing")

        # Watermark must advance
        ws = app.session_compactions["sk_cap1"]
        self.assertEqual(ws["summarized_through"], 3, "watermark did not advance")
        self.assertFalse(ws["in_flight"])

    # ---- (2) assistant tool_call args capped at 300 --------------------------
    async def test_tool_call_args_capped(self):
        task = "bbbb0000-0000-4000-8000-000000000002"
        self._seed(task, {
            "session_key": "sk_cap2", "cut": 6, "cut_anchor": 0,
            "cut_prev_anchor": 0, "seed_sig": "s",
            "summarized_through": 0, "dropped_total": 2, "in_flight": True,
        })
        big_args = json.dumps({"query": "x" * 5000})
        msgs = [
            {"role": "user", "content": "Run the query."},
            {"role": "assistant", "content": "Running query.",
             "tool_calls": [{"id": "tc1", "type": "function",
                             "function": {"name": "run_sql", "arguments": big_args}}]},
        ]
        chunks_seen = []
        async def fake_call_4b(*a, **k):
            if k.get("json_mode"):
                return {"state_update": {"current_state":
                    "CURRENT TASK/PHASE: ran query. COMPLETED: executed sql. "
                    "IN PROGRESS: none. NEXT STEP: report. DECISIONS/CONSTRAINTS: none. "
                    "DO NOT REDO: query execution."}}
            content = a[0][0]["content"] if a and a[0] else ""
            chunks_seen.append(content)
            return "CURRENT TASK/PHASE: phase ok."

        with patch.object(app, "_call_4b", new=fake_call_4b):
            await app._summarize_trimmed_messages(task, "sk_cap2", msgs)

        all_text = "\n".join(chunks_seen)
        # The 5000-char arg must NOT appear in full
        self.assertNotIn(big_args, all_text, "full 5k tool_call arg leaked")
        # But the function name should be visible
        self.assertIn("run_sql", all_text, "tool_call function name missing")
        # User text intact
        self.assertIn("Run the query.", all_text)

    # ---- (3) env=0 disables the cap -----------------------------------------
    async def test_env_zero_disables_cap(self):
        task = "cccc0000-0000-4000-8000-000000000003"
        self._seed(task, {
            "session_key": "sk_cap3", "cut": 4, "cut_anchor": 0,
            "cut_prev_anchor": 0, "seed_sig": "s",
            "summarized_through": 0, "dropped_total": 2, "in_flight": True,
        })
        big_tool = "Z" * 5000
        msgs = [
            {"role": "user", "content": "Check the data."},
            {"role": "tool", "content": big_tool},
        ]

        chunks_seen = []
        async def fake_call_4b(*a, **k):
            if k.get("json_mode"):
                return {"state_update": {"current_state":
                    "CURRENT TASK/PHASE: checked data. COMPLETED: read tool. "
                    "IN PROGRESS: none. NEXT STEP: report. DECISIONS/CONSTRAINTS: none. "
                    "DO NOT REDO: data check."}}
            content = a[0][0]["content"] if a and a[0] else ""
            chunks_seen.append(content)
            return "CURRENT TASK/PHASE: phase ok."

        old_val = app.SUMMARY_TOOL_CAP_CHARS
        app.SUMMARY_TOOL_CAP_CHARS = 0
        try:
            with patch.object(app, "_call_4b", new=fake_call_4b):
                await app._summarize_trimmed_messages(task, "sk_cap3", msgs)
        finally:
            app.SUMMARY_TOOL_CAP_CHARS = old_val

        all_text = "\n".join(chunks_seen)
        # With cap=0, the full tool body should pass through
        # (the generic per_cap=500 will still truncate, but the 5000-char
        #  body won't have the specific "[N chars omitted]" marker from our cap)
        self.assertNotIn("chars omitted", all_text,
                         "tool cap marker should not appear when cap=0")

    # ---- (4) watermark + phase storage unchanged ----------------------------
    async def test_watermark_phase_storage_unchanged(self):
        task = "dddd0000-0000-4000-8000-000000000004"
        self._seed(task, {
            "session_key": "sk_cap4", "cut": 8, "cut_anchor": 0,
            "cut_prev_anchor": 0, "seed_sig": "s",
            "summarized_through": 0, "dropped_total": 4, "in_flight": True,
        })
        msgs = [
            {"role": "user", "content": "Step one."},
            {"role": "tool", "content": "R" * 3000},
            {"role": "assistant", "content": "Done step one."},
            {"role": "user", "content": "Step two now."},
        ]

        async def fake_call_4b(*a, **k):
            if k.get("json_mode"):
                return {"state_update": {"current_state":
                    "CURRENT TASK/PHASE: two steps done. COMPLETED: step one, step two. "
                    "IN PROGRESS: none. NEXT STEP: finish. DECISIONS/CONSTRAINTS: none. "
                    "DO NOT REDO: steps one and two."}}
            return "CURRENT TASK/PHASE: phase stored."

        with patch.object(app, "_call_4b", new=fake_call_4b):
            await app._summarize_trimmed_messages(task, "sk_cap4", msgs)

        ws = app.session_compactions["sk_cap4"]
        self.assertEqual(ws["summarized_through"], 4)
        self.assertFalse(ws["in_flight"])
        # Phase row must be stored
        phase_inserts = [e for e in app.pool.executes
                         if "phase_summaries" in e[0] and "INSERT" in e[0]]
        self.assertGreaterEqual(len(phase_inserts), 1, "no phase row stored")
        # Root summary must be stored
        root_inserts = [e for e in app.pool.executes
                        if "session_summaries" in e[0] and "INSERT" in e[0]]
        self.assertGreaterEqual(len(root_inserts), 1, "no root summary stored")


if __name__ == "__main__":
    unittest.main()
