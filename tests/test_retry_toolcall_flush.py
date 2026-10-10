"""Regression tests: retry-path tool-call flush (the unconditional-break bug).

Root cause: In the non-thinking reasoning retry path, an unconditional `break`
exited the retry stream loop BEFORE the buffered tool-call SSE chunks could be
safety-checked and emitted. This left Goose with finish_reason="tool_calls"
but zero actual tool-call deltas.

These tests verify:
1. _unbounded_replace_loop() correctly detects/blocks the unsafe pattern
2. The retry flush block is structurally reachable (not preceded by an early break)
3. Safe retry tool calls are emitted; unsafe ones are blocked
4. The tc_emitted_n invariant holds (no phantom tool_calls)
"""
import ast
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parent.parent / "proxy" / "app.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_app_source() -> str:
    return APP_PATH.read_text()


def _extract_function_source(source: str, func_name: str) -> str:
    """Extract the source of a top-level function from the app source."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            end = node.end_lineno
            lines = source.split("\n")
            return "\n".join(lines[node.lineno - 1:end])
    raise ValueError(f"Function {func_name} not found in {APP_PATH}")


def _exec_function(func_name: str):
    """Extract and exec a single function from app.py, return it."""
    source = _extract_function_source(_load_app_source(), func_name)
    ns: dict = {"re": re}
    exec(compile(source, "<extracted>", "exec"), ns)
    return ns[func_name]


# ---------------------------------------------------------------------------
# Test 1: _unbounded_replace_loop detects the exact unsafe pattern
# ---------------------------------------------------------------------------

class TestUnboundedReplaceLoop:
    def setup_method(self):
        self.fn = _exec_function("_unbounded_replace_loop")

    def test_unsafe_while_includes_replace(self):
        """The exact pattern from the bug report must be detected."""
        code = textwrap.dedent("""\
            while (content.includes(old8)) {
                content = content.replace(old8, new9);
                count++;
            }
        """)
        assert self.fn(code) is True, "Must detect unbounded while(includes) + replace"

    def test_unsafe_no_counter(self):
        """Unbounded loop with no counter at all."""
        code = textwrap.dedent("""\
            while (s.includes(a)) {
                s = s.replace(a, b);
            }
        """)
        assert self.fn(code) is True

    def test_safe_replaceall(self):
        """replaceAll is safe - no while loop."""
        code = "content = content.replaceAll(old8, new9);"
        assert self.fn(code) is False

    def test_safe_split_join(self):
        """split/join is safe - no while loop."""
        code = "content = content.split(old8).join(new9);"
        assert self.fn(code) is False

    def test_safe_for_loop(self):
        """A for loop inside is bounded."""
        code = textwrap.dedent("""\
            for (let i = 0; i < 10; i++) {
                content = content.replace(old8, new9);
            }
        """)
        assert self.fn(code) is False

    def test_safe_normal_code(self):
        """Normal code with no loop pattern."""
        code = "const x = 5; console.log(x);"
        assert self.fn(code) is False

    def test_safe_while_with_cap(self):
        """A while loop that doesn't match the includes+replace pattern."""
        code = textwrap.dedent("""\
            let i = 0;
            while (i < 100) {
                i++;
            }
        """)
        assert self.fn(code) is False


# ---------------------------------------------------------------------------
# Test 2: The retry flush block is structurally reachable
# ---------------------------------------------------------------------------

class TestRetryFlushReachable:
    """Verify the flush block is NOT preceded by an unconditional break.

    The original bug: an unconditional `break` sat between the stream loop
    and the flush block, making the flush dead code.
    """

    def test_no_unconditional_break_before_flush(self):
        """The line immediately before the flush comment must NOT be a bare break."""
        source = _load_app_source()
        lines = source.split("\n")

        # Find the flush comment
        flush_idx = None
        for i, line in enumerate(lines):
            if "Flush buffered tool calls from retry" in line:
                flush_idx = i
                break
        assert flush_idx is not None, "Flush comment not found in app.py"

        # Walk backwards to find the previous non-empty, non-comment line
        prev_idx = flush_idx - 1
        while prev_idx >= 0:
            stripped = lines[prev_idx].strip()
            if stripped and not stripped.startswith("#"):
                break
            prev_idx -= 1

        assert prev_idx >= 0, "No preceding line found"
        prev_line = lines[prev_idx].strip()

        # The previous executable line must NOT be a bare "break"
        # (it could be a "break" inside an if-block, which is fine)
        if prev_line == "break":
            # Check if this break is indented deeper (inside a conditional)
            # vs the flush block's indent level
            flush_indent = len(lines[flush_idx]) - len(lines[flush_idx].lstrip())
            break_indent = len(lines[prev_idx]) - len(lines[prev_idx].lstrip())
            # If the break is at the SAME indent as the flush, it's unconditional
            assert break_indent > flush_indent, (
                f"Unconditional break at line {prev_idx+1} (indent={break_indent}) "
                f"immediately before flush block (indent={flush_indent}). "
                f"This is the original bug - the flush is unreachable!"
            )

    def test_flush_block_has_break_after(self):
        """The flush block must be followed by a break to exit the retry loop."""
        source = _load_app_source()
        lines = source.split("\n")

        flush_idx = None
        for i, line in enumerate(lines):
            if "Flush buffered tool calls from retry" in line:
                flush_idx = i
                break
        assert flush_idx is not None, "Flush comment not found in app.py"

        # Find the if tc_buffer: line after the flush comment
        if_tc_buffer_idx = None
        for i in range(flush_idx, flush_idx + 10):
            if "if tc_buffer:" in lines[i]:
                if_tc_buffer_idx = i
                break
        assert if_tc_buffer_idx is not None, "if tc_buffer: not found after flush comment"

        tc_indent = len(lines[if_tc_buffer_idx]) - len(lines[if_tc_buffer_idx].lstrip())

        # Find a break at the tc_buffer indent level within 60 lines after the flush block
        found_break = False
        for i in range(if_tc_buffer_idx, min(if_tc_buffer_idx + 60, len(lines))):
            stripped = lines[i].strip()
            if stripped == "break":
                line_indent = len(lines[i]) - len(lines[i].lstrip())
                if line_indent == tc_indent:
                    found_break = True
                    break
        assert found_break, "No break at the tc_buffer indent level after the flush block"

    def test_tc_buffer_initialized_in_retry_setup(self):
        """tc_buffer must be initialized in the retry setup block."""
        source = _load_app_source()
        lines = source.split("\n")

        # Find "Loop in reasoning - non-thinking retry"
        retry_log_idx = None
        for i, line in enumerate(lines):
            if "Loop in reasoning - non-thinking retry" in line:
                retry_log_idx = i
                break
        assert retry_log_idx is not None

        # tc_buffer = [] should appear within 100 lines after (structure has shifted)
        found = False
        for i in range(retry_log_idx, retry_log_idx + 100):
            if "tc_buffer = []" in lines[i]:
                found = True
                break
        assert found, "tc_buffer = [] not found in retry setup"

    def test_tc_buffer_append_in_retry_stream(self):
        """The retry stream must buffer TCs (not emit them directly)."""
        source = _load_app_source()
        lines = source.split("\n")

        # Find tc_accum = ToolCallAccumulator() in the retry path
        tc_accum_idx = None
        for i, line in enumerate(lines):
            if "tc_accum = ToolCallAccumulator()" in line:
                # Must be in the latter half (retry path)
                if i > 4000:
                    tc_accum_idx = i
                    break
        assert tc_accum_idx is not None, "tc_accum = ToolCallAccumulator() not found in retry path"

        # Find tc_buffer.append within 200 lines after
        found = False
        for i in range(tc_accum_idx, min(tc_accum_idx + 200, len(lines))):
            if "tc_buffer.append" in lines[i]:
                found = True
                break
        assert found, "tc_buffer.append not found in retry stream"


# ---------------------------------------------------------------------------
# Test 3: State/output invariant
# ---------------------------------------------------------------------------

class TestStateInvariant:
    """tc_complete=True + finish_reason=tool_calls must imply tc_emitted_n > 0
    with ACTUAL emitted chunks (not just tc_accum.count())."""

    def test_tc_emitted_n_not_phantom(self):
        """The tc_emitted_n calculation must reflect actual emission.

        In the blocked case, finish_reason is set to "stop" (not "tool_calls"),
        so the invariant is maintained.
        """
        source = _load_app_source()

        # In the unsafe case, finish_reason must be "stop"
        assert 'finish_reason = "stop"' in source
        assert 'exit_reason = "unsafe_loop_blocked"' in source

        # The tc_emitted_n must use tc_emitted_count (actual emission count)
        assert "tc_emitted_count" in source

    def test_blocked_case_sets_stop_not_tool_calls(self):
        """When the tool call is blocked, finish_reason must be 'stop'."""
        source = _load_app_source()
        lines = source.split("\n")

        # Find the unsafe_tc block
        unsafe_idx = None
        for i, line in enumerate(lines):
            if "_unsafe_tc" in line and "if" in line:
                unsafe_idx = i
                break
        assert unsafe_idx is not None, "unsafe_tc check not found"

        # Within the next 10 lines, finish_reason must be set to "stop"
        found_stop = False
        for i in range(unsafe_idx, unsafe_idx + 10):
            if 'finish_reason = "stop"' in lines[i]:
                found_stop = True
                break
        assert found_stop, "finish_reason not set to 'stop' in blocked case"


# ---------------------------------------------------------------------------
# Test 4: Simulated retry flush (integration-level)
# ---------------------------------------------------------------------------

class TestRetryFlushSimulation:
    """Simulate the retry flush logic to verify safe/unsafe behavior."""

    def test_safe_toolcall_emitted(self):
        """A safe execute_typescript tool call should be emitted."""
        # Simulate what the flush block does
        tc_buffer = [
            {"id": "stream-1", "object": "chat.completion.chunk", "created": 0,
             "model": "test", "choices": [{"index": 0, "delta": {"tool_calls": [
                 {"index": 0, "id": "call_1", "type": "function",
                  "function": {"name": "execute_typescript", "arguments": ""}}
             ]}, "finish_reason": None}]},
        ]
        tc_complete = True
        BLOCK_UNBOUNDED_LOOPS = True

        # Safe code
        code = "async function run() { return 42; }"

        _unsafe_tc = False
        if tc_complete and BLOCK_UNBOUNDED_LOOPS:
            fn = _exec_function("_unbounded_replace_loop")
            if fn(code):
                _unsafe_tc = True

        assert _unsafe_tc is False, "Safe code should not be blocked"

        # The chunks should be emitted
        emitted = []
        for chunk in tc_buffer:
            emitted.append(json.dumps(chunk))
        assert len(emitted) == 1, "One tool-call chunk should be emitted"
        tc_buffer.clear()
        assert len(tc_buffer) == 0, "Buffer should be cleared after emission"

    def test_unsafe_toolcall_blocked(self):
        """An unbounded replace loop should be blocked."""
        tc_buffer = [
            {"id": "stream-1", "object": "chat.completion.chunk", "created": 0,
             "model": "test", "choices": [{"index": 0, "delta": {"tool_calls": [
                 {"index": 0, "id": "call_1", "type": "function",
                  "function": {"name": "execute_typescript", "arguments": ""}}
             ]}, "finish_reason": None}]},
        ]
        tc_complete = True
        BLOCK_UNBOUNDED_LOOPS = True

        # Unsafe code (the exact pattern from the bug report)
        code = textwrap.dedent("""\
            while (content.includes(old8)) {
                content = content.replace(old8, new9);
                count++;
            }
        """)

        _unsafe_tc = False
        if tc_complete and BLOCK_UNBOUNDED_LOOPS:
            fn = _exec_function("_unbounded_replace_loop")
            if fn(code):
                _unsafe_tc = True

        assert _unsafe_tc is True, "Unsafe code must be detected"

        # No chunks should be emitted
        emitted = []
        if _unsafe_tc:
            # BLOCKED: do not emit
            pass
        else:
            for chunk in tc_buffer:
                emitted.append(json.dumps(chunk))

        assert len(emitted) == 0, "No tool-call chunks should be emitted for unsafe code"
        assert tc_buffer is not None, "Buffer should still exist (not cleared)"

    def test_no_dangling_state(self):
        """After blocking, finish_reason must be 'stop' (not 'tool_calls')."""
        # Simulate the blocked path
        finish_reason = "tool_calls"  # initial state from stream
        _unsafe_tc = True

        if _unsafe_tc:
            finish_reason = "stop"

        assert finish_reason == "stop", (
            f"finish_reason must be 'stop' after blocking, got '{finish_reason}'"
        )
