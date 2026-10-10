"""The incomplete-chunk suppression fix (latest)."""

from tests.fakes import (
    run_stream,
    toolcall_payloads,
    terminal_event,
    terminal_ctxgate,
    script_incomplete_tc_normal,
    script_reasoning_overflow,
    script_safe_tc_normal,
)


def test_IC1_main_incomplete_tc_not_emitted():
    events = run_stream([script_incomplete_tc_normal()])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 0, f"expected 0 deltas, got {len(deltas)}"
    g = terminal_ctxgate(events)
    assert g.get("tool_calls_complete") is False, f"got {g}"
    assert g.get("tool_calls_emitted") == 0, f"got {g}"
    assert g.get("tool_call_truncated") is True, f"got {g}"
    f = terminal_event(events)
    assert f["choices"][0]["finish_reason"] == "length", \
        f"got {f['choices'][0]['finish_reason']}"
    assert events[-1]["kind"] == "done", "last event is not [DONE]"


def test_IC2_main_complete_tc_still_emitted():
    events = run_stream([script_safe_tc_normal()])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 3, f"expected 3 deltas, got {len(deltas)}"
    g = terminal_ctxgate(events)
    assert g.get("tool_calls_complete") is True, f"got {g}"
    assert g.get("tool_calls_emitted") == 3, f"got {g}"
    assert g.get("tool_call_truncated") is not True, f"got {g}"
    f = terminal_event(events)
    assert f["choices"][0]["finish_reason"] == "tool_calls", \
        f"got {f['choices'][0]['finish_reason']}"


def test_IC3_retry_complete_tc_emitted():
    events = run_stream([
        script_reasoning_overflow(),
        script_safe_tc_normal(),
    ])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 3, f"expected 3 deltas, got {len(deltas)}"
    g = terminal_ctxgate(events)
    assert g.get("tool_calls_complete") is True, f"got {g}"
    assert g.get("tool_calls_emitted") == 3, f"got {g}"
    assert g.get("tool_call_truncated") is not True, f"got {g}"


def test_IC4_incomplete_tc_does_not_advance_emitted_count():
    """Two runs in a row: safe then incomplete. The second must not
    inherit the first run's emission."""
    from proxy import app as P

    run_stream([script_safe_tc_normal()])
    events = run_stream([script_incomplete_tc_normal()])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 0, f"expected 0 deltas, got {len(deltas)}"
    g = terminal_ctxgate(events)
    assert g.get("tool_calls_emitted") == 0, f"got {g}"
    assert g.get("tool_calls_complete") is False, f"got {g}"
    assert g.get("tool_call_truncated") is True, f"got {g}"
