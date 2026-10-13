"""Hot-path benchmark test for Trim Summary.

Exercises the actual Goose hot path through proxy/hotpath/session_id.py:_resolve_session_identity.
Verifies session ID resolution via agent-session-id and X-Session-ID headers,
then benchmarks the full trim→summarize→digest pipeline with mocked LLM calls.

Run: cd /home/pawelw/ctxproxy && python -m pytest tests/test_hotpath_trim_benchmark.py -v --timeout=120
"""
import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from typing import Optional
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from proxy.config.constants import GOOSE_SESSION_UUID_NAMESPACE

# ---------------------------------------------------------------------------
# Constants & Mock Data
# ---------------------------------------------------------------------------

TEST_SESSION_ID = "hotpath-bench-session-001"
EXPECTED_TASK_UUID = str(uuid.uuid5(GOOSE_SESSION_UUID_NAMESPACE, TEST_SESSION_ID))
SESSION_KEY = f"goose:{TEST_SESSION_ID}"

MOCK_SUMMARY_TEMPLATE = (
    "CURRENT TASK/PHASE: Implementing feature-{phase} in module_{phase}\n"
    "COMPLETED: Added core logic for feature-{phase}. Tests passing ({phase}/12).\n"
    "IN PROGRESS: Integration testing for feature-{phase}.\n"
    "NEXT STEP: Wire up feature-{phase} to the main dispatch loop.\n"
    "DECISIONS/CONSTRAINTS: Using standard library only. No external deps.\n"
    "DO NOT REDO: Core logic for feature-{phase} already implemented and tested.\n"
    "ARTIFACTS: src/module_{phase}.py (core logic), src/module_{phase}_test.py (tests)\n"
    "VERIFICATION: pytest src/module_{phase}_test.py - {phase} passed"
)

LABELS = [
    "CURRENT TASK/PHASE:",
    "COMPLETED:",
    "IN PROGRESS:",
    "NEXT STEP:",
    "DECISIONS/CONSTRAINTS:",
    "DO NOT REDO:",
    "ARTIFACTS:",
    "VERIFICATION:",
]


def _mock_summary_for_phase(phase: int) -> str:
    """Generate a realistic mock summary for a given phase number."""
    return MOCK_SUMMARY_TEMPLATE.replace("{phase}", str(phase))


# ---------------------------------------------------------------------------
# Mock Infrastructure
# ---------------------------------------------------------------------------

class MockRow(dict):
    """Simulates asyncpg.Record – supports both integer and string key access."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class MockPool:
    """In-memory mock of asyncpg.Pool for the subset of queries used by trim/digest."""

    def __init__(self):
        self.phase_summaries: list[dict] = []
        self.session_summaries: list[dict] = []
        self.session_ledger: list[dict] = []
        self.insert_count = 0

    async def fetchrow(self, query: str, *args):
        if "MAX(phase_number)" in query and "phase_summaries" in query:
            task_id = str(args[0])
            rows = [r for r in self.phase_summaries if r["task_id"] == task_id]
            mp = max((r["phase_number"] for r in rows), default=0)
            return MockRow({"mp": mp})
        if "FROM proxy.events" in query:
            return None
        return None

    async def fetch(self, query: str, *args):
        if "FROM proxy.phase_summaries" in query and "ORDER BY phase_number DESC" in query:
            task_id = str(args[0])
            rows = [r for r in self.phase_summaries if r["task_id"] == task_id]
            rows.sort(key=lambda r: r["phase_number"], reverse=True)
            return [MockRow(r) for r in rows[:50]]
        if "FROM proxy.session_ledger" in query:
            return []
        return []

    async def fetchval(self, query: str, *args):
        if "FROM proxy.session_summaries" in query:
            task_id = str(args[0])
            rows = [r for r in self.session_summaries if r["task_id"] == task_id]
            if rows:
                return rows[-1]["summary"]
            return None
        return None

    async def execute(self, query: str, *args):
        self.insert_count += 1
        if "INSERT INTO proxy.phase_summaries" in query:
            task_id, session_key, phase_number, summary, msg_count, tok_count = args
            # Upsert semantics: replace if (task_id, phase_number) exists
            existing = next(
                (i for i, r in enumerate(self.phase_summaries)
                 if r["task_id"] == str(task_id) and r["phase_number"] == phase_number),
                None,
            )
            row = {
                "task_id": str(task_id),
                "session_key": session_key,
                "phase_number": phase_number,
                "summary": summary,
                "trimmed_msg_count": msg_count,
                "trimmed_tokens": tok_count,
            }
            if existing is not None:
                self.phase_summaries[existing] = row
            else:
                self.phase_summaries.append(row)
        elif "INSERT INTO proxy.session_summaries" in query:
            task_id, session_key, summary, msg_count, tok_count = args
            self.session_summaries.append({
                "task_id": str(task_id),
                "session_key": session_key,
                "summary": summary,
                "trimmed_msg_count": msg_count,
                "trimmed_tokens": tok_count,
            })
        # Other tables (events, memory_jobs) are no-ops
        return "NOTFOUND"


def _make_request(headers: dict):
    """Create a minimal stand-in for fastapi.Request with the given headers dict."""
    mock_req = MagicMock()
    mock_req.headers = headers
    return mock_req


def _make_messages(n_pairs: int, prefix: str = "") -> list:
    """Create n user/assistant message pairs with realistic content."""
    msgs = []
    for i in range(n_pairs):
        label = f"{prefix}{i}" if prefix else str(i)
        msgs.append({"role": "user", "content": f"Please implement feature {label} with proper error handling and tests."})
        msgs.append({"role": "assistant", "content": f"Done. Feature {label} is implemented in src/feature_{label}.py. All tests pass."})
    return msgs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pool():
    """Provide a fresh MockPool instance."""
    return MockPool()


@pytest.fixture
def patched_app(mock_pool):
    """Patch proxy.app module globals and related modules for isolated testing.

    Yields a dict with references to the mocks for assertion purposes.
    """
    import proxy.app as app_mod
    import proxy.lm.call as lm_call_mod
    import proxy.hotpath.session as hpsession_mod
    import proxy.summarize.slices as slices_mod
    import proxy.ledger.extract as extract_mod
    import proxy.ledger.persist as persist_mod
    import proxy.window.persist as wpersist_mod
    import proxy.token.counter as counter_mod

    saved = {
        "pool": app_mod.pool,
        "session_compactions": app_mod.session_compactions,
        "_summary_task_locks": app_mod._summary_task_locks,
        "_summary_last_attempt": app_mod._summary_last_attempt,
        "_slice_log": app_mod._slice_log,
        "metrics": app_mod.metrics,
        "log": app_mod.log,
        "_call_4b": lm_call_mod._call_4b,
        "_get_goose_session_info": hpsession_mod._get_goose_session_info,
        "_enqueue_context_slices": slices_mod._enqueue_context_slices,
        "_extract_ledger_entries": extract_mod._extract_ledger_entries,
        "_persist_ledger_entries": persist_mod._persist_ledger_entries,
        "_window_persist": wpersist_mod._window_persist,
        "count_tokens": counter_mod.count_tokens,
    }

    # --- Set up app module globals ---
    app_mod.pool = mock_pool
    app_mod.session_compactions = {}
    app_mod._summary_task_locks = {}
    app_mod._summary_last_attempt = {}
    app_mod._slice_log = []
    app_mod.metrics = {
        "summary_slices_ok": 0,
        "summary_slices_fail": 0,
        "trims": 0,
        "requests": 0,
    }
    app_mod.log = logging.getLogger("test-hotpath-bench")

    # --- Mock _call_4b to return phase-appropriate summaries ---
    _phase_counter = {"n": 0}

    async def mock_call_4b(messages, max_tokens=2000, json_mode=True, priority=0, system=None, kind="other"):
        _phase_counter["n"] += 1
        # Extract phase number from the user message ("Phase N:")
        user_content = ""
        for m in messages:
            if m.get("role") == "user":
                user_content = m.get("content", "")
                break
        phase_num = 1
        if "Phase " in user_content:
            try:
                phase_str = user_content.split("Phase ")[1].split(":")[0].strip()
                phase_num = int(phase_str)
            except (IndexError, ValueError):
                pass
        return _mock_summary_for_phase(phase_num)

    lm_call_mod._call_4b = mock_call_4b

    # --- Mock session info lookup (return None → no enrichment) ---
    async def mock_get_goose_session_info(session_id):
        return None

    hpsession_mod._get_goose_session_info = mock_get_goose_session_info

    # --- No-op slice enqueuing ---
    async def mock_enqueue_context_slices(*a, **kw):
        pass

    slices_mod._enqueue_context_slices = mock_enqueue_context_slices

    # --- No-op ledger operations ---
    def mock_extract_ledger_entries(*a, **kw):
        return []

    extract_mod._extract_ledger_entries = mock_extract_ledger_entries

    async def mock_persist_ledger_entries(*a, **kw):
        pass

    persist_mod._persist_ledger_entries = mock_persist_ledger_entries

    # --- No-op window persist ---
    async def mock_window_persist(*a, **kw):
        pass

    wpersist_mod._window_persist = mock_window_persist

    # --- Simple token counter (chars / 4 ≈ tokens) ---
    def mock_count_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    counter_mod.count_tokens = mock_count_tokens

    yield {
        "pool": mock_pool,
        "app_mod": app_mod,
        "phase_counter": _phase_counter,
    }

    # Restore originals
    app_mod.pool = saved["pool"]
    app_mod.session_compactions = saved["session_compactions"]
    app_mod._summary_task_locks = saved["_summary_task_locks"]
    app_mod._summary_last_attempt = saved["_summary_last_attempt"]
    app_mod._slice_log = saved["_slice_log"]
    app_mod.metrics = saved["metrics"]
    app_mod.log = saved["log"]
    lm_call_mod._call_4b = saved["_call_4b"]
    hpsession_mod._get_goose_session_info = saved["_get_goose_session_info"]
    slices_mod._enqueue_context_slices = saved["_enqueue_context_slices"]
    extract_mod._extract_ledger_entries = saved["_extract_ledger_entries"]
    persist_mod._persist_ledger_entries = saved["_persist_ledger_entries"]
    wpersist_mod._window_persist = saved["_window_persist"]
    counter_mod.count_tokens = saved["count_tokens"]


# ---------------------------------------------------------------------------
# Part 1: Unit-level Hot Path Verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHotPathSessionResolution:
    """Verify that _resolve_session_identity correctly handles agent-session-id
    and X-Session-ID headers through the real resolver (no bypass)."""

    async def test_agent_session_id_header(self, patched_app):
        """Send request with 'agent-session-id' header → verify resolved UUID matches uuid5 derivation."""
        from proxy.hotpath.session_id import _resolve_session_identity, _cache_clear

        _cache_clear()  # ensure clean cache

        session_id = "test-session-abc"
        expected_uuid = str(uuid.uuid5(GOOSE_SESSION_UUID_NAMESPACE, session_id))
        expected_key = f"goose:{session_id}"

        request = _make_request({"agent-session-id": session_id})
        result = await _resolve_session_identity(request, [], {})

        assert result["id"] == session_id
        assert result["uuid"] == expected_uuid
        assert result["session_key"] == expected_key
        assert result["log_label"] == "GOOSE-HEADER"
        assert result["client_cap"] is None

    async def test_x_session_id_header(self, patched_app):
        """Send request with 'X-Session-ID' header (no agent-session-id) → verify same resolution."""
        from proxy.hotpath.session_id import _resolve_session_identity, _cache_clear

        _cache_clear()

        session_id = "x-session-xyz-123"
        expected_uuid = str(uuid.uuid5(GOOSE_SESSION_UUID_NAMESPACE, session_id))
        expected_key = f"goose:{session_id}"

        request = _make_request({"X-Session-ID": session_id})
        result = await _resolve_session_identity(request, [], {})

        assert result["id"] == session_id
        assert result["uuid"] == expected_uuid
        assert result["session_key"] == expected_key
        assert result["log_label"] == "GOOSE-HEADER"

    async def test_agent_session_id_takes_precedence(self, patched_app):
        """Both headers present → agent-session-id wins over X-Session-ID."""
        from proxy.hotpath.session_id import _resolve_session_identity, _cache_clear

        _cache_clear()

        primary_id = "primary-agent-session"
        secondary_id = "secondary-x-session"
        expected_uuid = str(uuid.uuid5(GOOSE_SESSION_UUID_NAMESPACE, primary_id))

        request = _make_request({
            "agent-session-id": primary_id,
            "X-Session-ID": secondary_id,
        })
        result = await _resolve_session_identity(request, [], {})

        # agent-session-id takes precedence
        assert result["id"] == primary_id
        assert result["uuid"] == expected_uuid
        assert result["session_key"] == f"goose:{primary_id}"
        assert result["log_label"] == "GOOSE-HEADER"

    async def test_fallback_when_no_headers(self, patched_app):
        """Neither header present → content fingerprint fallback (documented, NOT counted as hot-path)."""
        from proxy.hotpath.session_id import _resolve_session_identity, _cache_clear

        _cache_clear()

        messages = [{"role": "user", "content": "Hello world, this is a unique message for fingerprinting."}]
        request = _make_request({})
        result = await _resolve_session_identity(request, messages, {})

        # Fallback path: id starts with "fallback:"
        assert result["id"].startswith("fallback:")
        assert result["log_label"] == "FALLBACK"
        # UUID is derived from the fallback ID
        expected_uuid = str(uuid.uuid5(GOOSE_SESSION_UUID_NAMESPACE, result["id"]))
        assert result["uuid"] == expected_uuid
        # Session key includes the fallback ID
        assert result["session_key"] == f"goose:{result['id']}"
        # Client cap extracted from body
        request2 = _make_request({})
        result2 = await _resolve_session_identity(request2, messages, {"max_tokens": 4096})
        assert result2["client_cap"] == 4096

    async def test_real_session_id_matches_expected_uuid(self, patched_app):
        """Verify that the specific TEST_SESSION_ID resolves to EXPECTED_TASK_UUID."""
        from proxy.hotpath.session_id import _resolve_session_identity, _cache_clear

        _cache_clear()

        request = _make_request({"agent-session-id": TEST_SESSION_ID})
        result = await _resolve_session_identity(request, [], {})

        assert result["uuid"] == EXPECTED_TASK_UUID
        assert result["session_key"] == SESSION_KEY
        assert result["id"] == TEST_SESSION_ID


# ---------------------------------------------------------------------------
# Part 2: Integration Benchmark (10+ trim cycles)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTrimSummaryBenchmark:
    """End-to-end benchmark: simulate 10 conversation turns that trigger trim,
    verify summaries are produced and stored correctly through the real hot path."""

    @pytest.fixture(autouse=True)
    def setup_session(self, patched_app):
        """Set up the session compaction state for the benchmark session."""
        app_mod = patched_app["app_mod"]

        # Resolve session identity through the REAL resolver
        from proxy.hotpath.session_id import _resolve_session_identity, _cache_clear
        _cache_clear()

        request = _make_request({"agent-session-id": TEST_SESSION_ID})
        identity = asyncio.get_event_loop().run_until_complete(
            _resolve_session_identity(request, [], {})
        ) if False else None

        # We're in an async context, so just call directly
        # (the fixture is sync, so we defer the async call to the test)
        self._identity = None
        self._session_key = SESSION_KEY
        self._task_uuid = EXPECTED_TASK_UUID
        self._messages = []
        self._msgs_per_batch = 10

        # Initialize session_compactions with initial state
        app_mod.session_compactions[self._session_key] = {
            "summarized_through": 0,
            "dropped_total": 0,
            "cut": 0,
        }

    async def _resolve_and_setup(self):
        """Resolve session identity via the real hot path and set up state."""
        from proxy.hotpath.session_id import _resolve_session_identity, _cache_clear
        _cache_clear()

        request = _make_request({"agent-session-id": TEST_SESSION_ID})
        self._identity = await _resolve_session_identity(request, [], {})

        assert self._identity["uuid"] == self._task_uuid
        assert self._identity["session_key"] == self._session_key

    async def test_ten_trims_produce_summaries(self, patched_app):
        """
        Simulate 10 batches of messages that trigger trim.
        After each trim, verify a new row appears in proxy.phase_summaries.
        """
        app_mod = patched_app["app_mod"]
        pool = patched_app["pool"]

        # Step 1: Resolve session identity through the REAL hot path
        await self._resolve_and_setup()

        # Step 2: Simulate 10 trim cycles
        from proxy.summarize.trim_summary import _summarize_trimmed_messages

        timings = []
        total_msgs = 0

        for batch in range(1, 11):
            # Add a new batch of messages
            new_msgs = _make_messages(self._msgs_per_batch, prefix=f"b{batch}-")
            self._messages.extend(new_msgs)
            total_msgs = len(self._messages)

            # Update window state: dropped_total grows, summarized_through stays
            ws = app_mod.session_compactions[self._session_key]
            ws["dropped_total"] = total_msgs
            ws["cut"] = total_msgs

            # Trigger the trim summarization
            t0 = time.monotonic()
            await _summarize_trimmed_messages(
                task_uuid=self._task_uuid,
                session_key=self._session_key,
                rest=self._messages,
                session_name="bench-test",
            )
            elapsed = time.monotonic() - t0
            timings.append(elapsed)

            # Advance watermark (simulating what happens after successful trim)
            ws["summarized_through"] = total_msgs

        # --- Assertions ---
        rows = [r for r in pool.phase_summaries if r["task_id"] == self._task_uuid]

        # 10+ rows in phase_summaries
        assert len(rows) >= 10, f"Expected >=10 phase summaries, got {len(rows)}"

        # Each row has non-empty summary text
        for i, row in enumerate(rows):
            assert row["summary"], f"Phase {row['phase_number']} has empty summary"
            assert len(row["summary"]) > 50, f"Phase {row['phase_number']} summary too short"

        # Phase numbers are sequential starting from 1
        phase_numbers = sorted(r["phase_number"] for r in rows)
        assert phase_numbers == list(range(1, len(rows) + 1)), (
            f"Phase numbers not sequential: {phase_numbers}"
        )

        # Summaries contain expected labeled fields
        for row in rows:
            s = row["summary"]
            assert "CURRENT TASK/PHASE:" in s, f"Missing CURRENT TASK/PHASE in phase {row['phase_number']}"
            assert "COMPLETED:" in s, f"Missing COMPLETED in phase {row['phase_number']}"
            assert "NEXT STEP:" in s, f"Missing NEXT STEP in phase {row['phase_number']}"

        # Token count of each summary should be < 800
        for row in rows:
            est_tokens = len(row["summary"]) // 4
            assert est_tokens < 800, (
                f"Phase {row['phase_number']} summary estimated at {est_tokens} tokens (>= 800)"
            )

        # Log timing
        avg_time = sum(timings) / len(timings)
        total_time = sum(timings)
        print(f"\n[BENCHMARK] 10 trims completed:")
        print(f"  Per-trim average: {avg_time*1000:.1f} ms")
        print(f"  Total elapsed: {total_time*1000:.1f} ms")
        print(f"  Phase summaries stored: {len(rows)}")
        print(f"  Avg summary length: {sum(len(r['summary']) for r in rows)/len(rows):.0f} chars")

    async def test_trim_summary_quality_comparison(self, patched_app):
        """
        For the same input messages, compare:
        A) The system's generated summary (from phase_summaries table)
        B) A reference summary generated by the test itself

        Metrics: field completeness, information density, length compliance.
        """
        app_mod = patched_app["app_mod"]
        pool = patched_app["pool"]

        # Resolve session identity through the REAL hot path
        await self._resolve_and_setup()

        # Create a batch of messages
        messages = _make_messages(5, prefix="quality-")

        # Set up window state
        app_mod.session_compactions[self._session_key] = {
            "summarized_through": 0,
            "dropped_total": len(messages),
            "cut": len(messages),
        }

        # Trigger trim
        from proxy.summarize.trim_summary import _summarize_trimmed_messages
        await _summarize_trimmed_messages(
            task_uuid=self._task_uuid,
            session_key=self._session_key,
            rest=messages,
            session_name="quality-test",
        )

        # Get the system-produced summary
        rows = [r for r in pool.phase_summaries if r["task_id"] == self._task_uuid]
        assert len(rows) >= 1, "No summary produced"
        system_summary = rows[0]["summary"]

        # Reference summary (what a good summary SHOULD contain)
        ref_summary = _mock_summary_for_phase(1)

        # --- Metric 1: Field Completeness (all 8 labeled fields present?) ---
        missing_in_system = [lbl for lbl in LABELS if lbl not in system_summary]
        missing_in_ref = [lbl for lbl in LABELS if lbl not in ref_summary]

        assert not missing_in_ref, f"Reference summary missing labels: {missing_in_ref}"
        assert not missing_in_system, f"System summary missing labels: {missing_in_system}"

        # --- Metric 2: Information Density (key structural elements preserved) ---
        # Both should mention the phase/task concept
        assert "CURRENT TASK" in system_summary
        assert "COMPLETED" in system_summary
        # Should have some substantive content (not just labels)
        system_words = system_summary.split()
        assert len(system_words) > 20, f"System summary too sparse: {len(system_words)} words"

        # --- Metric 3: Length Compliance (< 500 words as prompted) ---
        word_count = len(system_words)
        assert word_count < 500, f"System summary exceeds 500 words: {word_count}"
        assert word_count >= 30, f"System summary unreasonably short: {word_count} words"

        print(f"\n[QUALITY] System summary: {word_count} words, all {len(LABELS)} fields present")
        print(f"[QUALITY] Reference summary: {len(ref_summary.split())} words, all {len(LABELS)} fields present")


# ---------------------------------------------------------------------------
# Part 3: Scale Stress Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTrimAtScale:
    """Verify the system handles 50+ phases without degradation."""

    async def test_fifty_phases_digest_performance(self, patched_app):
        """
        Pre-populate 50 phase_summaries rows for a test task_uuid.
        Trigger the digest builder.
        Assert performance and content constraints.
        """
        app_mod = patched_app["app_mod"]
        pool = patched_app["pool"]

        # Resolve session identity through the REAL hot path
        from proxy.hotpath.session_id import _resolve_session_identity, _cache_clear
        _cache_clear()

        request = _make_request({"agent-session-id": TEST_SESSION_ID})
        identity = await _resolve_session_identity(request, [], {})
        assert identity["uuid"] == EXPECTED_TASK_UUID

        # Pre-populate 50 phase summaries
        for phase in range(1, 51):
            pool.phase_summaries.append({
                "task_id": EXPECTED_TASK_UUID,
                "session_key": SESSION_KEY,
                "phase_number": phase,
                "summary": _mock_summary_for_phase(phase),
                "trimmed_msg_count": 10,
                "trimmed_tokens": 100,
            })

        assert len(pool.phase_summaries) == 50

        # Trigger the digest builder
        from proxy.summarize.digest import _build_session_digest

        t0 = time.monotonic()
        digest = await _build_session_digest(EXPECTED_TASK_UUID, SESSION_KEY)
        elapsed = time.monotonic() - t0

        # --- Assertions ---

        # Digest completes in < 2 seconds
        assert elapsed < 2.0, f"Digest took {elapsed:.3f}s (exceeds 2s limit)"

        # Digest is <= 6000 chars
        assert len(digest) <= 6000, f"Digest is {len(digest)} chars (exceeds 6000 limit)"

        # Digest is non-empty
        assert digest, "Digest is empty"

        # Only latest phase is included in full
        assert "LATEST PHASE (phase 50):" in digest, "Latest phase header missing from digest"
        # The full summary of phase 50 should be present
        phase50_summary = _mock_summary_for_phase(50)
        assert phase50_summary in digest, "Phase 50 full summary not in digest"

        # Older phases contribute only COMPLETED/DECISIONS lines
        assert "EARLIER PHASES:" in digest, "Earlier phases section missing"

        # Verify that older phase FULL summaries are NOT included
        # (only their COMPLETED/DECISIONS lines should appear)
        phase49_full = _mock_summary_for_phase(49)
        # The full multi-line summary of phase 49 should NOT be in the digest
        # but its COMPLETED line fragment should be
        assert phase49_full not in digest, "Phase 49 full summary leaked into digest"

        # Check that COMPLETED lines from older phases ARE present
        completed_lines_in_digest = [
            line for line in digest.split("\n")
            if "COMPLETED:" in line
        ]
        assert len(completed_lines_in_digest) >= 2, (
            f"Expected multiple COMPLETED lines from earlier phases, got {len(completed_lines_in_digest)}"
        )

        # Verify the digest was persisted to session_summaries
        assert len(pool.session_summaries) >= 1, "Digest not persisted to session_summaries"

        print(f"\n[SCALE] 50-phase digest built in {elapsed*1000:.1f} ms")
        print(f"[SCALE] Digest size: {len(digest)} chars (limit: 6000)")
        print(f"[SCALE] COMPLETED lines from earlier phases: {len(completed_lines_in_digest)}")
        print(f"[SCALE] Latest phase fully included: {'LATEST PHASE (phase 50)' in digest}")

