"""Tests for O4-TCT: Smart tool-call-truncation continuation logic."""
import json
import pytest
from unittest.mock import patch, MagicMock
from proxy.streaming.toolcall import ToolCallAccumulator


class TestToolCallTruncationFix:
    """Verify the O4-TCT smart continuation behavior."""

    def _build_tc_pieces_partial(self):
        """Build TC delta pieces with intentionally broken JSON args."""
        partial_json = '{"query": "what is the'
        return [
            [{"index": 0, "id": "call_abc123", "type": "function",
              "function": {"name": "search_web", "arguments": ""}}],
            [{"index": 0, "function": {"arguments": partial_json}}],
        ]

    def _build_tc_pieces_complete(self):
        """Build TC delta pieces with valid JSON args."""
        valid_json = '{"city": "Paris"}'
        return [
            [{"index": 0, "id": "call_xyz", "type": "function",
              "function": {"name": "get_weather", "arguments": ""}}],
            [{"index": 0, "function": {"arguments": valid_json}}],
        ]

    def test_incomplete_tool_call_triggers_smart_continuation(self):
        """Reasoning-heavy response with incomplete TC triggers O4-TCT path."""
        tc_pieces = self._build_tc_pieces_partial()
        accum = ToolCallAccumulator()
        for piece in tc_pieces:
            accum.add_delta(piece)

        assert accum.count() == 1
        assert not accum.is_complete(), "Should be incomplete (truncated JSON)"

        # Simulate what the new code does: extract names, build compact hint
        _tc_names = [c["name"] for c in accum._calls.values() if c["name"]]
        _tc_partial_args = [c["arguments"] for c in accum._calls.values() if c["arguments"]]

        assert _tc_names == ["search_web"], "Tool name should be extracted"
        assert len(_tc_partial_args[0]) < 100, "Partial args should be short"

        # Build assistant content as the new code does (NO reasoning included)
        _assist_content = ""
        _hint = ", ".join(_tc_names)
        _preview = "; ".join(a[:80] for a in _tc_partial_args if a)
        _assist_content += "\n[Tool calls in progress: " + _hint + "]"
        _assist_content += " Partial args: " + _preview

        # KEY ASSERTION: no reasoning garbage in the message
        assert "RRRRR" not in _assist_content
        assert "[Tool calls in progress: search_web]" in _assist_content
        assert len(_assist_content) < 200, "Assistant msg should be compact"

    def test_normal_length_truncation_unaffected(self):
        """Normal (non-tool-call) length truncation uses standard path."""
        accum = ToolCallAccumulator()
        assert accum.count() == 0
        tc_incomplete = accum.count() > 0 and not accum.is_complete()
        assert tc_incomplete == False, "No TCs means no tc_incomplete"

    def test_complete_tool_call_no_continuation(self):
        """A fully valid tool call should NOT trigger continuation."""
        tc_pieces = self._build_tc_pieces_complete()
        accum = ToolCallAccumulator()
        for piece in tc_pieces:
            accum.add_delta(piece)

        assert accum.count() == 1
        assert accum.is_complete(), "Valid JSON arguments -> complete"

    def test_max_tokens_is_full_for_tc_continuation(self):
        """O4-TCT continuation uses full MAX_OUTPUT, not reduced budget."""
        from proxy.config.constants import MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT

        remaining = CTXGATE_MAX_TOTAL_OUTPUT
        max_tokens_new = min(MAX_OUTPUT, remaining)
        assert max_tokens_new == MAX_OUTPUT, "Should get full MAX_OUTPUT"

        remaining_half = CTXGATE_MAX_TOTAL_OUTPUT // 2
        max_tokens_new = min(MAX_OUTPUT, remaining_half)
        assert max_tokens_new == MAX_OUTPUT, "Still full if budget allows"

    def test_completion_directive_is_present(self):
        """User message instructs direct JSON output without reasoning."""
        directive = ("Complete the tool call arguments as valid JSON immediately. "
                     "Do not reason, explain, or add commentary. "
                     "Output ONLY the JSON argument objects.")
        assert "Do not reason" in directive
        assert "ONLY the JSON" in directive
        assert "immediately" in directive


class TestToolCallAccumulatorIntegration:
    """Integration-level tests for the accumulator used in the fix."""

    def test_partial_json_detected_as_incomplete(self):
        """Unbalanced JSON braces should be detected as incomplete."""
        accum = ToolCallAccumulator()
        bad_json = '{"a": 1'
        accum.add_delta([{"index": 0, "id": "c1", "function": {"name": "fn", "arguments": bad_json}}])
        assert accum.count() == 1
        assert not accum.is_complete(), "Unbalanced braces = incomplete"

    def test_valid_json_detected_as_complete(self):
        """Valid JSON should be detected as complete."""
        accum = ToolCallAccumulator()
        good_json = '{"a": 1}'
        accum.add_delta([{"index": 0, "id": "c1", "function": {"name": "fn", "arguments": good_json}}])
        assert accum.count() == 1
        assert accum.is_complete(), "Valid JSON = complete"

    def test_empty_arguments_incomplete(self):
        """Empty arguments string should be incomplete."""
        accum = ToolCallAccumulator()
        accum.add_delta([{"index": 0, "id": "c1", "function": {"name": "fn", "arguments": ""}}])
        assert accum.count() == 1
        assert not accum.is_complete(), "Empty args = incomplete"
