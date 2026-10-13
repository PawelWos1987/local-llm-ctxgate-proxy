"""Tests for Fix 1 (RHO_COLD), Fix 2 (_parse_vllm_actual_tokens), Fix 3 (escalating truncation)."""
import pytest
from proxy.config.constants import RHO_COLD, MAX_CONTEXT, MAX_INPUT
from proxy.window.cut import _parse_vllm_actual_tokens


# ─── Fix 1: RHO_COLD accuracy ────────────────────────────────────────────────

class TestRhoCold:
    def test_rho_cold_is_4_5(self):
        """RHO_COLD should be 4.5 for mixed code/text content."""
        assert RHO_COLD == 4.5

    def test_estimate_280kb_payload(self):
        """A 280KB payload should estimate to ~62K tokens with RHO_COLD=4.5."""
        payload_chars = 280 * 1024  # 280KB
        estimated = payload_chars // RHO_COLD
        # Should be in range 60K-70K (matching vLLM's ~65-68K)
        assert 55_000 < estimated < 75_000, f"Estimated {estimated}, expected 55K-75K"


# ─── Fix 2: _parse_vllm_actual_tokens ────────────────────────────────────────

class TestParseVllmActualTokens:
    def test_full_error_format(self):
        """Parse the exact vLLM error format from session 20261013_4."""
        error_body = (
            '{"error":{"message":"This model\'s maximum context length is 83200 tokens. '
            'However, you requested 15000 output tokens and your prompt contains '
            'at least 68201 input tokens, for a total of at least 83201 tokens."}}'
        )
        result = _parse_vllm_actual_tokens(error_body)
        
        assert result["max_context"] == 83200
        assert result["requested_output"] == 15000
        assert result["actual_input"] == 68201
        assert result["total"] == 83201

    def test_compute_overage(self):
        """The caller computes overage = actual_input + requested_output - max_context."""
        error_body = (
            "This model's maximum context length is 83200 tokens. "
            "However, you requested 15000 output tokens and your prompt contains "
            "at least 68201 input tokens, for a total of at least 83201 tokens."
        )
        result = _parse_vllm_actual_tokens(error_body)
        
        overage = result["actual_input"] + result["requested_output"] - result["max_context"]
        assert overage == 1  # 68201 + 15000 - 83200 = 1

    def test_large_overage_scenario(self):
        """When a huge tool output pushes well over budget."""
        error_body = (
            "This model's maximum context length is 83200 tokens. "
            "However, you requested 15000 output tokens and your prompt contains "
            "at least 95000 input tokens, for a total of at least 110000 tokens."
        )
        result = _parse_vllm_actual_tokens(error_body)
        
        assert result["max_context"] == 83200
        assert result["requested_output"] == 15000
        assert result["actual_input"] == 95000
        assert result["total"] == 110000
        
        overage = result["actual_input"] + result["requested_output"] - result["max_context"]
        assert overage == 26800  # Way more than 1 token!

    def test_partial_parse_fallback(self):
        """If only some fields are present, return what we can."""
        error_body = "maximum context length is 83200 tokens"
        result = _parse_vllm_actual_tokens(error_body)
        
        assert result["max_context"] == 83200
        assert result["requested_output"] is None
        assert result["actual_input"] is None
        assert result["total"] is None

    def test_no_match_returns_none_fields(self):
        """Unrelated error returns all None."""
        result = _parse_vllm_actual_tokens("some random error message")
        
        assert result["max_context"] is None
        assert result["requested_output"] is None
        assert result["actual_input"] is None
        assert result["total"] is None


# ─── Fix 3: Escalating truncation logic ──────────────────────────────────────

class TestContextLengthError:
    """Tests for the simplified context-length error handling.

    Phase A (hotpath/context.py) guarantees the payload fits within budget.
    Any context-length error from vLLM after Phase A sizing is unexpected
    and results in a simple 413 error rather than emergency recuts.
    """

    def test_error_type_is_context_length_exceeded(self):
        """The error response uses type=context_length_exceeded."""
        import inspect
        from proxy.streaming import to_vllm
        src = inspect.getsource(to_vllm.forward_to_vllm)
        assert "context_length_exceeded" in src
        assert "status_code=413" in src

    def test_no_pre_send_gate_in_nonstream(self):
        """The PRE-SEND GATE block should not exist in to_vllm.py."""
        import inspect
        from proxy.streaming import to_vllm
        src = inspect.getsource(to_vllm.forward_to_vllm)
        assert "PRE-SEND GATE" not in src

    def test_no_pre_send_gate_in_stream(self):
        """The PRE-SEND GATE block should not exist in to_vllm_stream.py."""
        import inspect
        from proxy.streaming import to_vllm_stream
        src = inspect.getsource(to_vllm_stream.stream_to_vllm)
        assert "PRE-SEND GATE" not in src

    def test_no_hard_cut_in_error_path(self):
        """_hard_cut_to should not appear in the non-stream file."""
        import inspect
        from proxy.streaming import to_vllm
        src = inspect.getsource(to_vllm.forward_to_vllm)
        assert "_hard_cut_to" not in src

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
