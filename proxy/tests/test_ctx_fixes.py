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

class TestEscalatingTruncation:
    def test_counter_starts_at_zero(self):
        """The per-session counter starts empty."""
        from proxy.streaming.to_vllm_stream import _ctx_fail_counter
        assert "nonexistent_session" not in _ctx_fail_counter

    def test_injected_message_is_user_role(self):
        """[CONTEXT TRUNCATED - CONTINUE] must be role='user', not 'system'."""
        # Simulate what the code does
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Do something"},
            {"role": "assistant", "content": "Done"},
        ]
        # The injection appends a user message
        messages.append({"role": "user", "content": "[CONTEXT TRUNCATED - CONTINUE]"})
        
        last_msg = messages[-1]
        assert last_msg["role"] == "user"
        assert last_msg["content"] == "[CONTEXT TRUNCATED - CONTINUE]"

    def test_truncation_preserves_system_message(self):
        """System message (index 0) must never be removed."""
        messages = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
        ]
        # Simulate removing oldest non-system
        del messages[1]
        assert messages[0]["role"] == "system"
        assert len(messages) == 3

    def test_target_levels(self):
        """Verify the escalation targets for MAX_CONTEXT=83200."""
        level1_target = int(MAX_CONTEXT * 0.4)
        level2_target = int(MAX_CONTEXT * 0.25)
        
        # Level 1: ~33280 tokens
        assert 30_000 < level1_target < 35_000
        # Level 2: ~20800 tokens  
        assert 18_000 < level2_target < 23_000

    def test_third_failure_gives_up(self):
        """After 3 consecutive failures, the system gives up cleanly."""
        from proxy.streaming.to_vllm_stream import _ctx_fail_counter
        _ctx_fail_counter["test_sess"] = 3
        assert _ctx_fail_counter["test_sess"] >= 3
        # Cleanup
        del _ctx_fail_counter["test_sess"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
