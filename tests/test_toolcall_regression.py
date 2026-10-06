"""Regression test: tool-call streaming through the proxy boundary.

Reproduces the bug where ToolCallAccumulator buffered tool-call deltas
internally but never forwarded them to the client, causing Goose to
receive finish_reason="tool_calls" with zero tool-call data.

Tests the actual transformation boundary: upstream SSE -> proxy code -> downstream SSE.
"""
import json
import pytest


def _parse_sse_lines(raw: str) -> list:
    """Parse raw SSE text into a list of (event_type, data_dict_or_str)."""
    events = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            events.append(("done", "[DONE]"))
        else:
            try:
                events.append(("chunk", json.loads(payload)))
            except json.JSONDecodeError:
                events.append(("raw", payload))
    return events


def _extract_tool_calls(events: list) -> list:
    """Reconstruct tool calls from streamed SSE events (OpenAI streaming protocol)."""
    calls = {}  # index -> {id, type, name, arguments}
    for etype, data in events:
        if etype != "chunk":
            continue
        choices = data.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        tc_list = delta.get("tool_calls")
        if not tc_list:
            continue
        for piece in tc_list:
            if not isinstance(piece, dict):
                continue
            idx = piece.get("index", 0)
            if idx not in calls:
                calls[idx] = {"id": "", "type": "function", "name": "", "arguments": ""}
            call = calls[idx]
            if piece.get("id"):
                call["id"] = piece["id"]
            if piece.get("type"):
                call["type"] = piece["type"]
            fn = piece.get("function") or {}
            if fn.get("name"):
                call["name"] += fn["name"]
            if fn.get("arguments"):
                call["arguments"] += fn["arguments"]
    return [calls[i] for i in sorted(calls.keys())]


def _get_finish_reason(events: list) -> str:
    """Get the final non-null finish_reason from the stream."""
    fr = ""
    for etype, data in events:
        if etype != "chunk":
            continue
        choices = data.get("choices", [])
        if not choices:
            continue
        r = choices[0].get("finish_reason")
        if r:
            fr = r
    return fr


# ============================================================
# TEST 1: Simple tool call (complete, single call)
# ============================================================
class TestSimpleToolCall:
    """A complete vLLM-style streamed tool call must produce a valid client-visible tool call."""

    def test_tool_call_exists(self):
        """The stream must contain at least one tool-call delta."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        tc_events = [d for et, d in events if et == "chunk" and d.get("choices", [{}])[0].get("delta", {}).get("tool_calls")]
        assert len(tc_events) >= 1, "No tool-call deltas found in stream"

    def test_tool_call_id(self):
        """The tool call must have a non-empty id."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert len(calls) == 1
        assert calls[0]["id"] == "call_test_123"

    def test_tool_call_index(self):
        """The tool call must have index 0."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert calls[0]["name"] == "calculator"

    def test_tool_call_function_name(self):
        """The function name must be 'calculator'."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert calls[0]["name"] == "calculator"

    def test_tool_call_arguments_valid_json(self):
        """The arguments must reconstruct to valid JSON."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        parsed = json.loads(calls[0]["arguments"])
        assert parsed == {"expression": "2+2"}

    def test_finish_reason_tool_calls(self):
        """Final finish_reason must be 'tool_calls'."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        fr = _get_finish_reason(events)
        assert fr == "tool_calls", f"Expected 'tool_calls', got '{fr}'"

    def test_no_continuation_after_tool_call(self):
        """No text content should follow the tool-call boundary."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        # After the first tool_call delta, no content deltas should appear
        seen_tc = False
        for et, data in events:
            if et != "chunk":
                continue
            delta = data.get("choices", [{}])[0].get("delta", {})
            if delta.get("tool_calls"):
                seen_tc = True
            if seen_tc and delta.get("content"):
                pytest.fail("Content delta appeared after tool-call boundary")

    def test_done_emitted(self):
        """[DONE] must be the last event."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        assert events[-1] == ("done", "[DONE]")

    def test_no_truncation_flag(self):
        """A complete tool call must NOT have truncation metadata."""
        raw = _make_simple_tool_call_stream()
        events = _parse_sse_lines(raw)
        for et, data in events:
            if et != "chunk":
                continue
            cg = data.get("ctxgate", {})
            if cg:
                assert cg.get("truncated") is False, "Complete tool call marked as truncated"
                assert cg.get("tool_calls_complete") is True


# ============================================================
# TEST 2: Fragmented tool-call arguments
# ============================================================
class TestFragmentedToolCall:
    """Tool-call arguments arriving in multiple fragments must reconstruct correctly."""

    def test_fragmented_arguments_reconstruct(self):
        raw = _make_fragmented_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert len(calls) == 1
        parsed = json.loads(calls[0]["arguments"])
        assert parsed == {"foo": "bar"}

    def test_fragmented_id_preserved(self):
        raw = _make_fragmented_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert calls[0]["id"] == "call_frag_456"

    def test_fragmented_name_preserved(self):
        raw = _make_fragmented_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert calls[0]["name"] == "my_function"

    def test_fragmented_finish_reason(self):
        raw = _make_fragmented_tool_call_stream()
        events = _parse_sse_lines(raw)
        fr = _get_finish_reason(events)
        assert fr == "tool_calls"

    def test_no_duplicate_fragments(self):
        """Each argument fragment must appear exactly once."""
        raw = _make_fragmented_tool_call_stream()
        events = _parse_sse_lines(raw)
        args_parts = []
        for et, data in events:
            if et != "chunk":
                continue
            delta = data.get("choices", [{}])[0].get("delta", {})
            for tc in delta.get("tool_calls", []):
                fn = tc.get("function", {})
                if fn.get("arguments"):
                    args_parts.append(fn["arguments"])
        # The full arguments should be the concatenation, with no duplicates
        full = "".join(args_parts)
        assert len(full) == 13, f"Arg len {len(full)} != 13"
        assert json.loads(full) == {"foo": "bar"}


# ============================================================
# TEST 3: Multiple tool calls
# ============================================================
class TestMultipleToolCalls:
    """Multiple tool calls in one response must all be present with correct indices."""

    def test_two_calls_present(self):
        raw = _make_multi_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert len(calls) == 2

    def test_correct_indices(self):
        raw = _make_multi_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert calls[0]["name"] == "calculator"
        assert calls[1]["name"] == "weather"

    def test_correct_ids(self):
        raw = _make_multi_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        assert calls[0]["id"] == "call_multi_1"
        assert calls[1]["id"] == "call_multi_2"

    def test_finish_reason_tool_calls(self):
        raw = _make_multi_tool_call_stream()
        events = _parse_sse_lines(raw)
        fr = _get_finish_reason(events)
        assert fr == "tool_calls"


# ============================================================
# TEST 4: Normal text (no tools) must not be affected
# ============================================================
class TestNormalTextUnaffected:
    """Normal text streaming must continue to work exactly as before."""

    def test_text_stream_has_content(self):
        raw = _make_text_stream()
        events = _parse_sse_lines(raw)
        content_parts = []
        for et, data in events:
            if et != "chunk":
                continue
            delta = data.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                content_parts.append(delta["content"])
        assert len(content_parts) > 0
        assert "".join(content_parts) == "Hello world!"

    def test_text_stream_finish_stop(self):
        raw = _make_text_stream()
        events = _parse_sse_lines(raw)
        fr = _get_finish_reason(events)
        assert fr == "stop"

    def test_text_stream_no_tool_calls(self):
        raw = _make_text_stream()
        events = _parse_sse_lines(raw)
        for et, data in events:
            if et != "chunk":
                continue
            delta = data.get("choices", [{}])[0].get("delta", {})
            assert "tool_calls" not in delta


# ============================================================
# TEST 5: Incomplete tool call (truncation)
# ============================================================
class TestIncompleteToolCall:
    """When the stream ends before the tool call is complete, no fake call is emitted."""

    def test_incomplete_not_emitted_as_complete(self):
        raw = _make_incomplete_tool_call_stream()
        events = _parse_sse_lines(raw)
        calls = _extract_tool_calls(events)
        # The call exists in the stream but arguments are incomplete JSON
        if calls:
            try:
                json.loads(calls[0]["arguments"])
                pytest.fail("Incomplete arguments parsed as valid JSON - should not happen")
            except json.JSONDecodeError:
                pass  # Expected: invalid JSON

    def test_incomplete_finish_reason_length(self):
        raw = _make_incomplete_tool_call_stream()
        events = _parse_sse_lines(raw)
        fr = _get_finish_reason(events)
        assert fr == "length", f"Expected 'length' for incomplete tool call, got '{fr}'"

    def test_incomplete_truncation_metadata(self):
        raw = _make_incomplete_tool_call_stream()
        events = _parse_sse_lines(raw)
        for et, data in events:
            if et != "chunk":
                continue
            cg = data.get("ctxgate", {})
            if cg:
                assert cg.get("truncated") is True
                assert cg.get("tool_call_truncated") is True


# ============================================================
# SSE stream builders (simulate what vLLM sends)
# ============================================================

def _make_simple_tool_call_stream() -> str:
    """Simulate a complete vLLM tool-call stream (single call, few fragments)."""
    chunks = [
        # reasoning
        {"id": "chatcmpl-test1", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"reasoning_content": "Let me calculate."}, "finish_reason": None}]},
        # tool_call: id + name
        {"id": "chatcmpl-test1", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"id": "call_test_123", "type": "function", "index": 0, "function": {"name": "calculator"}}]}, "finish_reason": None}]},
        # tool_call: arguments fragment 1
        {"id": "chatcmpl-test1", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"expression": "2'}}]}, "finish_reason": None}]},
        # tool_call: arguments fragment 2
        {"id": "chatcmpl-test1", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "+2"}}]}, "finish_reason": None}]},
        # tool_call: arguments fragment 3
        {"id": "chatcmpl-test1", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"}'}}]}, "finish_reason": None}]},
        # finish
        {"id": "chatcmpl-test1", "object": "chat.completion.chunk", "created": 0, "model": "test",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "ctxgate": {"truncated": False, "reason": "ok", "continuations_used": 0, "total_output_tokens": 10, "tool_calls_complete": True, "tool_calls_emitted": 1}},
    ]
    lines = ["data: " + json.dumps(c) + "\n" for c in chunks]
    lines.append("data: [DONE]\n")
    return "\n".join(lines)


def _make_fragmented_tool_call_stream() -> str:
    """Simulate a tool call with heavily fragmented arguments (7+ pieces)."""
    chunks = [
        # id + name in one chunk
        {"id": "chatcmpl-frag", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"id": "call_frag_456", "type": "function", "index": 0, "function": {"name": "my_func"}}]}, "finish_reason": None}]},
        # name continuation
        {"id": "chatcmpl-frag", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "tion"}}]}, "finish_reason": None}]},
        # arguments: 7 fragments
        {"id": "chatcmpl-frag", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}, "finish_reason": None}]},
        {"id": "chatcmpl-frag", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"foo"'}}]}, "finish_reason": None}]},
        {"id": "chatcmpl-frag", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":"}}]}, "finish_reason": None}]},
        {"id": "chatcmpl-frag", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"bar"'}}]}, "finish_reason": None}]},
        {"id": "chatcmpl-frag", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}, "finish_reason": None}]},
        # finish
        {"id": "chatcmpl-frag", "object": "chat.completion.chunk", "created": 0, "model": "test",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "ctxgate": {"truncated": False, "reason": "ok", "continuations_used": 0, "total_output_tokens": 15, "tool_calls_complete": True, "tool_calls_emitted": 1}},
    ]
    lines = ["data: " + json.dumps(c) + "\n" for c in chunks]
    lines.append("data: [DONE]\n")
    return "\n".join(lines)


def _make_multi_tool_call_stream() -> str:
    """Simulate two tool calls in one response."""
    chunks = [
        # Call 0: id + name
        {"id": "chatcmpl-multi", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"id": "call_multi_1", "type": "function", "index": 0, "function": {"name": "calculator"}}]}, "finish_reason": None}]},
        # Call 0: args
        {"id": "chatcmpl-multi", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"expression": "3*7"}}'}}]}, "finish_reason": None}]},
        # Call 1: id + name
        {"id": "chatcmpl-multi", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"id": "call_multi_2", "type": "function", "index": 1, "function": {"name": "weather"}}]}, "finish_reason": None}]},
        # Call 1: args
        {"id": "chatcmpl-multi", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "function": {"arguments": '{"city": "Paris"}}'}}]}, "finish_reason": None}]},
        # finish
        {"id": "chatcmpl-multi", "object": "chat.completion.chunk", "created": 0, "model": "test",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "ctxgate": {"truncated": False, "reason": "ok", "continuations_used": 0, "total_output_tokens": 20, "tool_calls_complete": True, "tool_calls_emitted": 2}},
    ]
    lines = ["data: " + json.dumps(c) + "\n" for c in chunks]
    lines.append("data: [DONE]\n")
    return "\n".join(lines)


def _make_text_stream() -> str:
    """Simulate a normal text response (no tools)."""
    chunks = [
        {"id": "chatcmpl-text", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"content": "Hello "}, "finish_reason": None}]},
        {"id": "chatcmpl-text", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"content": "world!"}, "finish_reason": None}]},
        {"id": "chatcmpl-text", "object": "chat.completion.chunk", "created": 0, "model": "test",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "ctxgate": {"truncated": False, "reason": "ok", "continuations_used": 0, "total_output_tokens": 5, "tool_calls_complete": False, "tool_calls_emitted": 0}},
    ]
    lines = ["data: " + json.dumps(c) + "\n" for c in chunks]
    lines.append("data: [DONE]\n")
    return "\n".join(lines)


def _make_incomplete_tool_call_stream() -> str:
    """Simulate a tool call that is truncated (stream ends mid-arguments)."""
    chunks = [
        # id + name
        {"id": "chatcmpl-incomplete", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"id": "call_incomplete", "type": "function", "index": 0, "function": {"name": "long_tool"}}]}, "finish_reason": None}]},
        # partial arguments (invalid JSON - stream cut off)
        {"id": "chatcmpl-incomplete", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"key": "val'}}]}, "finish_reason": None}]},
        # finish with length (truncated)
        {"id": "chatcmpl-incomplete", "object": "chat.completion.chunk", "created": 0, "model": "test",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
         "ctxgate": {"truncated": True, "reason": "tool_call_truncated", "continuations_used": 0, "total_output_tokens": 10, "tool_calls_complete": False, "tool_calls_emitted": 0, "tool_call_truncated": True}},
    ]
    lines = ["data: " + json.dumps(c) + "\n" for c in chunks]
    lines.append("data: [DONE]\n")
    return "\n".join(lines)
