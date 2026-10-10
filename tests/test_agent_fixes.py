"""The three in-flight fixes the agent made during the test cycle."""

from tests.fakes import (
    run_stream,
    toolcall_payloads,
    terminal_event,
    terminal_ctxgate,
    script_safe_tc_transport_dies,
    script_unsafe_tc_transport_dies,
    script_reasoning_overflow,
    script_safe_tc_normal,
)


def test_AF1_main_safe_tc_transport_dies():
    """AF1: main-loop flush sets exit state before the interrupted guard."""
    events = run_stream([script_safe_tc_transport_dies()])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 3, f"expected 3 deltas, got {len(deltas)}"
    g = terminal_ctxgate(events)
    assert g.get("reason") == "tool_calls_complete", f"got {g}"
    assert g.get("truncated") is False, f"got {g}"
    assert g.get("tool_calls_complete") is True, f"got {g}"
    f = terminal_event(events)
    assert f["choices"][0]["finish_reason"] == "tool_calls", \
        f"got {f['choices'][0]['finish_reason']}"
    assert events[-1]["kind"] == "done", "last event is not [DONE]"


def test_AF2_main_unsafe_tc_transport_dies():
    """AF2: main-loop guard protects unsafe_loop_blocked too."""
    events = run_stream([script_unsafe_tc_transport_dies()])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 0, f"expected 0 deltas, got {len(deltas)}"
    g = terminal_ctxgate(events)
    assert g.get("reason") == "unsafe_loop_blocked", f"got {g}"
    assert g.get("truncated") is True, f"got {g}"
    assert g.get("tool_calls_complete") is False, f"got {g}"
    f = terminal_event(events)
    assert f["choices"][0]["finish_reason"] == "stop", \
        f"got {f['choices'][0]['finish_reason']}"


def test_AF3_ladder_prefers_complete_tc_over_reasoning_flag():
    """AF3: retry safe TC wins over the segment-1 reasoning flag."""
    events = run_stream([
        script_reasoning_overflow(),
        script_safe_tc_normal(),
    ])
    g = terminal_ctxgate(events)
    assert g.get("reason") == "tool_calls_complete", \
        f"ladder did not prefer completed tool call: {g}"
    assert g.get("reason") != "reasoning_overflow", f"got {g}"
    assert g.get("reason") != "loop_recovered", f"got {g}"
