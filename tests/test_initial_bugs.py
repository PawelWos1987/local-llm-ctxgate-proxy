"""Tests A, B, C, D — the four initial defects."""

import asyncio

from tests.fakes import (
    run_stream,
    toolcall_payloads,
    terminal_event,
    terminal_ctxgate,
    script_reasoning_overflow,
    script_safe_tc_normal,
    script_safe_tc_transport_dies,
    script_unsafe_tc_transport_dies,
)


def test_A_insert_event_row_returns_tuple():
    """A: _insert_event_row returns (id, seq) with a non-null int seq."""
    from proxy import app as P

    logged = []

    class FakeConn:
        def transaction(self):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def cm():
                yield

            return cm()

        async def execute(self, q, *a):
            logged.append(("execute", q, a))

        async def fetchval(self, q, *a):
            logged.append(("fetchval", q, a))
            if "COALESCE(MAX(seq)" in q:
                return 7
            if "RETURNING id" in q:
                return 42
            return None

    class FakeAcq:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *exc):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcq()

    P.pool = FakePool()
    try:
        result = asyncio.run(P._insert_event_row(
            "00000000-0000-0000-0000-000000000001", "user", "hi"))
    finally:
        P.pool = None

    assert isinstance(result, tuple), f"got {type(result)}"
    assert result == (42, 7), f"got {result}"

    for kind, q, _a in logged:
        assert "SELECT seq FROM proxy.events WHERE id=" not in q, \
            f"forbidden extra SELECT present: {q}"
    assert any("FOR UPDATE" in q for _k, q, _a in logged), \
        "FOR UPDATE on proxy.tasks missing"


def test_A_enqueue_context_slices_passes_seq():
    """A: each context_slice INSERT supplies a non-null int seq."""
    from proxy import app as P

    logged = []

    class FakeConn:
        def transaction(self):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def cm():
                yield

            return cm()

        async def execute(self, q, *a):
            logged.append(("execute", q, a))

        async def fetchval(self, q, *a):
            logged.append(("fetchval", q, a))
            if "COALESCE(MAX(seq)" in q:
                return 5
            if "RETURNING id" in q:
                return 100
            return None

        async def fetchrow(self, q, *a):
            return None

    class FakeAcq:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *exc):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcq()

        async def execute(self, q, *a):
            logged.append(("execute", q, a))

        async def fetchrow(self, q, *a):
            return None

    P.pool = FakePool()
    try:
        asyncio.run(P._enqueue_context_slices(
            "00000000-0000-0000-0000-000000000001",
            "sk", ["a", "b", "c"], 0, 30))
    finally:
        P.pool = None

    inserts = [(q, a) for k, q, a in logged
               if k == "fetchval" and "INSERT INTO proxy.events" in q]
    assert len(inserts) == 3, f"expected 3 inserts, got {len(inserts)}"
    for q, a in inserts:
        assert "seq" in q, f"seq missing from column list: {q}"
        assert isinstance(a[1], int) and a[1] is not None, \
            f"seq arg is not an int: {a}"


def test_B_retry_safe_tc_normal_end():
    """B: retry segment's complete safe TC is flushed to the client."""
    events = run_stream([
        script_reasoning_overflow(),
        script_safe_tc_normal(),
    ])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 3, f"expected 3 tool-call deltas, got {len(deltas)}"

    f = terminal_event(events)
    assert f is not None, "no terminal chunk"
    assert f["choices"][0]["finish_reason"] == "tool_calls", \
        f"got {f['choices'][0]['finish_reason']}"

    g = terminal_ctxgate(events)
    assert g.get("tool_calls_complete") is True, f"got {g}"
    assert g.get("tool_calls_emitted") == 3, f"got {g}"

    assert events[-1]["kind"] == "done", "last event is not [DONE]"


def test_C_emitted_equals_delta_count_safe():
    """C: tool_calls_emitted equals the number of delta.tool_calls seen."""
    events = run_stream([
        script_reasoning_overflow(),
        script_safe_tc_normal(),
    ])
    deltas = toolcall_payloads(events)
    g = terminal_ctxgate(events)
    assert g.get("tool_calls_emitted") == len(deltas), \
        f"emitted={g.get('tool_calls_emitted')} deltas={len(deltas)}"


def test_C_unsafe_tc_suppresses_emission():
    """C: unsafe TC -> tool_calls_emitted == 0, tool_calls_complete is False."""
    events = run_stream([
        script_reasoning_overflow(),
        script_unsafe_tc_transport_dies(),
    ])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 0, f"expected 0 deltas, got {len(deltas)}"
    g = terminal_ctxgate(events)
    assert g.get("tool_calls_emitted") == 0, f"got {g}"
    assert g.get("tool_calls_complete") is False, f"got {g}"


def test_D_safe_retry_not_loop_recovered():
    """D: safe retry TC is reported as tool_calls_complete, truncated=False."""
    events = run_stream([
        script_reasoning_overflow(),
        script_safe_tc_normal(),
    ])
    g = terminal_ctxgate(events)
    assert g.get("reason") == "tool_calls_complete", f"got {g}"
    assert g.get("truncated") is False, f"got {g}"

    triple = (g.get("reason"), g.get("truncated"),
              terminal_event(events)["choices"][0]["finish_reason"])
    assert triple != ("loop_recovered", True, "tool_calls"), \
        f"forbidden tuple reached: {triple}"
