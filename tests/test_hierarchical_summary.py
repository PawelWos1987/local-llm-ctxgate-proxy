"""Tests for hierarchical/chaptered summarization (phase_summaries + root distillation)."""
import json
import os
import re
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))


class TestHierarchicalSummarization(unittest.TestCase):
    """Verify the hierarchical summarization architecture in app.py."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "proxy", "app.py")) as f:
            self.src = f.read()

    def test_phase_summaries_table_in_schema(self):
        """Schema migration creates proxy.phase_summaries table."""
        schema_path = os.path.join(os.path.dirname(__file__), "..", "schema", "011_phase_summaries.sql")
        self.assertTrue(os.path.exists(schema_path), "011_phase_summaries.sql missing")
        with open(schema_path) as f:
            sql = f.read()
        self.assertIn("CREATE TABLE IF NOT EXISTS proxy.phase_summaries", sql)
        self.assertIn("phase_number", sql)
        self.assertIn("UNIQUE(task_id, phase_number)", sql)


    def test_phase_summaries_insert(self):
        """Phase summaries are persisted to proxy.phase_summaries."""
        start = self.src.index("async def _summarize_trimmed_messages")
        end = self.src.index("async def _fetch_session_summary")
        func = self.src[start:end]
        self.assertIn("INSERT INTO proxy.phase_summaries", func)
        self.assertIn("ON CONFLICT (task_id, phase_number) DO UPDATE", func)



    def test_fetch_session_summary_budget_1500(self):
        """_fetch_session_summary default budget is 1500 tokens."""
        self.assertIn("async def _fetch_session_summary(task_uuid, budget=1500", self.src)

    def test_compact_context_fs_6000(self):
        """_compact_context frozen summary is capped at 6000 chars (not 2000)."""
        self.assertIn("fs[:6000]", self.src)
        self.assertNotIn("fs[:2000]", self.src)

    def test_fetch_task_memory_budgets_6000(self):
        """fetch_task_memory total budget is 6000 tokens."""
        self.assertIn("total_budget: int = 6000", self.src)
        self.assertIn("wm_budget: int = 1200", self.src)
        self.assertIn("mem_budget: int = 3300", self.src)

    def test_session_summary_injection_1500(self):
        """Session summary injection budget is 1500 tokens."""
        self.assertIn("_fetch_session_summary(task_uuid, budget=1500, session_key=session_id)", self.src)
        self.assertIn("if t <= 1500 and total + t <= total_budget:", self.src)

    def test_no_lm_studio_references(self):
        """No LM Studio references remain in the hierarchical code."""
        start = self.src.index("async def _summarize_trimmed_messages")
        end = self.src.index("async def _fetch_session_summary")
        func = self.src[start:end]
        self.assertNotIn("lm_studio", func.lower())
        self.assertNotIn("lmstudio", func.lower())
        self.assertNotIn("127.0.0.1:1234", func)

    def test_mistral_api_reachable(self):
        """Mistral API is reachable and responds to a minimal request."""
        import httpx
        api_key = ""
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("CTXGATE_LM_API_KEY="):
                        api_key = line.strip().split("=", 1)[1]
                        break
        if not api_key:
            self.skipTest("No CTXGATE_LM_API_KEY in .env")
        client = httpx.Client(timeout=30, headers={"Authorization": f"Bearer {api_key}"})
        r = client.post(
            "https://api.mistral.ai/v1/chat/completions",
            json={
                "model": "mistral-small-latest",
                "messages": [{"role": "user", "content": "Say OK"}],
                "max_tokens": 10,
            },
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("choices", data)
        self.assertTrue(len(data["choices"][0]["message"]["content"]) > 0)

    def test_phase_summaries_table_exists_in_db(self):
        """proxy.phase_summaries table exists in the live database."""
        import asyncpg
        dsn = ""
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("CTXGATE_DB_DSN="):
                        dsn = line.strip().split("=", 1)[1]
                        break
        if not dsn:
            self.skipTest("No CTXGATE_DB_DSN in .env")

        async def _check():
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
            try:
                row = await pool.fetchrow(
                    "SELECT COUNT(*) as cnt FROM information_schema.tables "
                    "WHERE table_schema='proxy' AND table_name='phase_summaries'"
                )
                return row["cnt"]
            finally:
                await pool.close()

        import asyncio
        cnt = asyncio.run(_check())
        self.assertEqual(cnt, 1, "proxy.phase_summaries table not found in DB")


if __name__ == "__main__":
    unittest.main()
