"""The four minor fixes."""

import asyncio
import pathlib
import re

from tests.fakes import (
    run_stream,
    toolcall_payloads,
    terminal_ctxgate,
    script_reasoning_overflow,
    script_safe_tc_normal,
    script_safe_tc_transport_dies,
)


def test_MF1_single_tool_call_complete_increment():
    """MF1: metrics['tool_call_complete'] advances by exactly 1."""
    from proxy import app as P

    before = P.metrics["tool_call_complete"]
    run_stream([script_reasoning_overflow(), script_safe_tc_normal()])
    after = P.metrics["tool_call_complete"]
    assert after - before == 1, f"delta={after - before}"


def test_MF2_no_dead_tc_emitted_variable():
    """MF2: the bare identifier tc_emitted is gone."""
    src = pathlib.Path("proxy/app.py").read_text()
    hits = re.findall(r"\btc_emitted\s*=\s*True\b", src)
    assert hits == [], f"found {len(hits)} occurrences of tc_emitted = True"


def test_MF3_enqueue_memory_job_has_no_extra_select():
    """MF3: _enqueue_memory_job does not issue an extra SELECT seq."""
    from proxy import app as P

    insert_calls = []

    async def fake_insert(task_uuid, role, content, meta=None):
        insert_calls.append((task_uuid, role, content))
        return ("evt-id", 5)

    async def fake_resolve(ref, create=False, tenant_id=""):
        return "00000000-0000-0000-0000-000000000001"

    async def fake_pending():
        return 0

    executed = []

    class FakePool:
        async def fetchrow(self, q, *a):
            return None

        async def execute(self, q, *a):
            executed.append((q, a))

    P.pool = FakePool()
    P._insert_event_row = fake_insert
    P._resolve_task = fake_resolve
    P._worker_pending_count = fake_pending
    try:
        asyncio.run(P._enqueue_memory_job("sess", "x" * 60))
    finally:
        P.pool = None

    assert len(insert_calls) == 1, f"insert called {len(insert_calls)} times"
    for q, _a in executed:
        assert "SELECT seq FROM proxy.events WHERE id=" not in q, \
            f"forbidden extra SELECT: {q}"


def test_MF4_retry_flush_before_interrupted_guard():
    """MF4: safe retry TC killed by transport error still flushes."""
    events = run_stream([
        script_reasoning_overflow(),
        script_safe_tc_transport_dies(),
    ])
    deltas = toolcall_payloads(events)
    assert len(deltas) == 3, f"expected 3 deltas, got {len(deltas)}"
    g = terminal_ctxgate(events)
    assert g.get("tool_calls_complete") is True, f"got {g}"
    assert g.get("tool_calls_emitted") == 3, f"got {g}"
    assert events[-1]["kind"] == "done", "last event is not [DONE]"
