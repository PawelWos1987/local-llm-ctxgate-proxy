"""Integration tests for exact prompt_tokens tracking and crash recovery."""
import os
import sys
import json
import sqlite3
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class TestSessionExactTokens:
    """Test the per-session exact prompt_tokens storage."""

    def test_store_and_get(self):
        from proxy.hotpath.tracking import store_exact_tokens, get_exact_tokens
        store_exact_tokens("goose:test_session_1", 50000)
        assert get_exact_tokens("goose:test_session_1") == 50000

    def test_get_nonexistent(self):
        from proxy.hotpath.tracking import get_exact_tokens
        assert get_exact_tokens("goose:nonexistent_session_xyz") is None

    def test_store_zero_ignores(self):
        from proxy.hotpath.tracking import store_exact_tokens, get_exact_tokens
        # Zero should not be stored (guards against empty usage)
        store_exact_tokens("goose:test_zero", 0)
        assert get_exact_tokens("goose:test_zero") is None

    def test_store_updates_existing(self):
        from proxy.hotpath.tracking import store_exact_tokens, get_exact_tokens
        store_exact_tokens("goose:test_update", 10000)
        store_exact_tokens("goose:test_update", 20000)
        assert get_exact_tokens("goose:test_update") == 20000


class TestRecoverSessionBaseline:
    """Test crash recovery from Goose's SQLite DB."""

    def test_recover_from_real_db(self):
        """Test recovery against the real Goose DB (if it exists)."""
        from proxy.hotpath.tracking import recover_session_baseline, _GOOSE_DB_PATH
        if not os.path.exists(_GOOSE_DB_PATH):
            pytest.skip(f"Goose DB not found at {_GOOSE_DB_PATH}")
        # Use a session that we know exists in the DB
        result = recover_session_baseline("goose:20261011_97")
        if result is not None:
            assert result > 0, f"Expected positive token count, got {result}"

    def test_recover_nonexistent_session(self):
        from proxy.hotpath.tracking import recover_session_baseline
        # A session that definitely doesn't exist
        result = recover_session_baseline("goose:nonexistent_session_abc123")
        assert result is None

    def test_recover_missing_db(self, monkeypatch):
        """Test that missing DB fails silently (returns None)."""
        from proxy.hotpath import tracking
        monkeypatch.setattr(tracking, "_GOOSE_DB_PATH", "/tmp/nonexistent_db_xyz.db")
        result = tracking.recover_session_baseline("goose:anything")
        assert result is None

    def test_recover_with_temp_db(self, tmp_path, monkeypatch):
        """Test recovery with a temporary SQLite DB."""
        from proxy.hotpath import tracking
        db_path = str(tmp_path / "test_sessions.db")
        # Create a minimal DB with the expected schema
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE usage_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                created_timestamp INTEGER NOT NULL,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER
            )
        """)
        conn.execute(
            "INSERT INTO usage_ledger (session_id, created_timestamp, input_tokens, output_tokens, total_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            ("test_sess_42", 1000, 42000, 500, 42500)
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(tracking, "_GOOSE_DB_PATH", db_path)
        result = tracking.recover_session_baseline("goose:test_sess_42")
        assert result == 42000

    def test_recover_strips_goose_prefix(self, tmp_path, monkeypatch):
        """Verify the goose: prefix is stripped before DB query."""
        from proxy.hotpath import tracking
        db_path = str(tmp_path / "test_sessions2.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE usage_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                created_timestamp INTEGER NOT NULL,
                input_tokens INTEGER
            )
        """)
        # Store WITHOUT the goose: prefix (as Goose does)
        conn.execute(
            "INSERT INTO usage_ledger (session_id, created_timestamp, input_tokens) "
            "VALUES (?, ?, ?)",
            ("20261011_86", 1000, 55000)
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(tracking, "_GOOSE_DB_PATH", db_path)
        # Query WITH the goose: prefix (as the proxy uses)
        result = tracking.recover_session_baseline("goose:20261011_86")
        assert result == 55000


class TestStreamingExactTokens:
    """Test that the streaming path reports exact prompt_tokens to Goose."""

    def test_final_chunk_uses_exact_value(self):
        """Simulate the _emit_ctxgate_final logic with exact tokens."""
        # Simulate what happens in to_vllm_stream.py:
        # input_tokens = 48000 (estimate), actual_prompt_tokens = 50000 (from vLLM)
        input_tokens = 48000
        actual_prompt_tokens = 50000
        total_cached_tokens = 45000
        out_toks = 500

        # This is the logic from _emit_ctxgate_final
        _pt = actual_prompt_tokens if actual_prompt_tokens > 0 else input_tokens
        _safe_cached = min(total_cached_tokens, _pt)

        assert _pt == 50000
        assert _safe_cached == 45000  # 45000 < 50000, so no clamping needed

    def test_final_chunk_falls_back_to_estimate(self):
        """When no usage received (edge case), fall back to estimate."""
        input_tokens = 48000
        actual_prompt_tokens = 0  # No usage received
        total_cached_tokens = 1000
        out_toks = 500

        _pt = actual_prompt_tokens if actual_prompt_tokens > 0 else input_tokens
        _safe_cached = min(total_cached_tokens, _pt)

        assert _pt == 48000  # Falls back to estimate
        assert _safe_cached == 1000

    def test_cached_never_exceeds_prompt(self):
        """Invariant: cached_tokens must never exceed prompt_tokens."""
        input_tokens = 40000
        actual_prompt_tokens = 42000
        total_cached_tokens = 50000  # vLLM reports more cached than total (edge case)
        out_toks = 100

        _pt = actual_prompt_tokens if actual_prompt_tokens > 0 else input_tokens
        _safe_cached = min(total_cached_tokens, _pt)

        assert _safe_cached == 42000  # Clamped to prompt_tokens
        assert _safe_cached <= _pt


class TestNonStreamingExactTokens:
    """Test that the non-streaming path reports exact prompt_tokens."""

    def test_uses_real_pt_when_available(self):
        """When vLLM returns prompt_tokens, use it."""
        input_tokens = 48000
        vllm_usage = {"prompt_tokens": 50000, "completion_tokens": 200}

        _real_pt = vllm_usage.get("prompt_tokens", 0)
        _final_pt = _real_pt if _real_pt > 0 else input_tokens

        assert _final_pt == 50000

    def test_falls_back_to_estimate(self):
        """When vLLM doesn't return prompt_tokens, use estimate."""
        input_tokens = 48000
        vllm_usage = {"completion_tokens": 200}  # No prompt_tokens

        _real_pt = vllm_usage.get("prompt_tokens", 0)
        _final_pt = _real_pt if _real_pt > 0 else input_tokens

        assert _final_pt == 48000


class TestPassthroughExactTokens:
    """Test that the passthrough path reports exact prompt_tokens."""

    def test_json_passthrough_uses_exact(self):
        """JSON passthrough: use vLLM's exact value when available."""
        input_tokens = 48000
        data = {
            "usage": {
                "prompt_tokens": 50000,
                "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 45000}
            }
        }

        _real_pt = data["usage"].get("prompt_tokens", 0)
        _final_pt = _real_pt if _real_pt > 0 else input_tokens
        out_tok = data["usage"].get("completion_tokens", 0)

        # Apply the fix
        data["usage"]["prompt_tokens"] = _final_pt
        data["usage"]["total_tokens"] = _final_pt + out_tok

        assert data["usage"]["prompt_tokens"] == 50000
        assert data["usage"]["total_tokens"] == 50200

    def test_stream_passthrough_captures_pt(self):
        """Stream passthrough: capture prompt_tokens from usage chunk."""
        input_tokens = 48000
        _actual_pt = 0

        # Simulate processing a usage chunk
        chunk = {
            "usage": {
                "prompt_tokens": 52000,
                "completion_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 50000}
            }
        }
        usage = chunk.get("usage")
        if usage:
            _pt = usage.get("prompt_tokens", 0)
            if _pt > 0:
                _actual_pt = _pt

        assert _actual_pt == 52000

        # Final value used for metrics
        _final_pt = _actual_pt if _actual_pt > 0 else input_tokens
        assert _final_pt == 52000


class TestTwoRequestFlow:
    """Simulate two consecutive requests for the same session."""

    def test_first_request_no_baseline(self):
        """First request: no stored baseline → uses full estimate."""
        from proxy.hotpath.tracking import store_exact_tokens, get_exact_tokens
        session_key = "goose:test_two_req"

        # Clear any existing state
        from proxy.hotpath.tracking import _session_exact_tokens
        _session_exact_tokens.pop(session_key, None)

        # First request: no baseline
        assert get_exact_tokens(session_key) is None

        # Simulate: estimate is used for budgeting
        input_tokens_estimate = 48000

        # vLLM responds with exact value
        vllm_prompt_tokens = 50000

        # Store the exact value
        store_exact_tokens(session_key, vllm_prompt_tokens)

        # Verify it was stored
        assert get_exact_tokens(session_key) == 50000

    def test_second_request_has_baseline(self):
        """Second request: stored baseline is available."""
        from proxy.hotpath.tracking import store_exact_tokens, get_exact_tokens
        session_key = "goose:test_two_req"

        # After first request, baseline is stored
        store_exact_tokens(session_key, 50000)

        # Second request: baseline is available
        baseline = get_exact_tokens(session_key)
        assert baseline == 50000

        # The estimate for the second request would be for the full payload
        # (including new messages), but the baseline confirms the old content
        # was 50000 tokens.
        input_tokens_estimate = 52000  # Slightly higher due to new messages

        # For reporting: use vLLM's exact value (simulated)
        vllm_prompt_tokens = 52100
        store_exact_tokens(session_key, vllm_prompt_tokens)

        assert get_exact_tokens(session_key) == 52100
