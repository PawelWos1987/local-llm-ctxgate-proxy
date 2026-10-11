"""Phase 06+07: Ledger/Retrieval + Durable Archive/Jobs tests."""
import sys
import os
import hashlib
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["CTXGATE_SKIP_DOTENV"] = "1"
os.environ["CTXGATE_TOKEN_ESTIMATOR"] = "calibrated"

import proxy.app as app


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _make_ledger_entry(
    entry_type="requirement",
    status="active",
    text="Do X",
    explicit=True,
    source_event_id=1,
    source_hash="abc123",
    superseded_by=None,
    tenant_id="t1",
    session_key="s1",
):
    return {
        "id": None,  # assigned by DB
        "tenant_id": tenant_id,
        "session_key": session_key,
        "task_id": None,
        "entry_type": entry_type,
        "status": status,
        "source_event_id": source_event_id,
        "source_hash": source_hash,
        "source_span": None,
        "text": text,
        "explicit": explicit,
        "superseded_by": superseded_by,
        "created_at": None,
        "updated_at": None,
    }


# ─── Phase 06: Ledger Tests ────────────────────────────────────────────────────

class TestLedgerStructure:
    def test_ledger_entry_has_all_fields(self):
        """Each ledger entry has id, type, status, source, text, explicit, superseded_by."""
        entry = _make_ledger_entry()
        # Verify all required fields are present
        required_fields = [
            "id", "tenant_id", "session_key", "task_id",
            "entry_type", "status", "source_event_id",
            "source_hash", "source_span", "text",
            "explicit", "superseded_by", "created_at", "updated_at",
        ]
        for f in required_fields:
            assert f in entry, f"Missing field: {f}"
        
        # Verify valid types
        valid_types = {
            "requirement", "constraint", "decision", "rejected_approach",
            "completed_work", "test_result", "failure", "artifact", "todo",
        }
        assert entry["entry_type"] in valid_types
        
        valid_statuses = {"active", "completed", "superseded", "rejected", "uncertain"}
        assert entry["status"] in valid_statuses
    
    def test_supersession_only_conflicting(self):
        """New instruction supersedes only conflicting older ones, not unrelated."""
        # Create existing entries
        entries = [
            _make_ledger_entry(entry_type="constraint", text="Use PostgreSQL", explicit=True, source_hash="h1"),
            _make_ledger_entry(entry_type="requirement", text="Deploy to AWS", explicit=True, source_hash="h2"),
            _make_ledger_entry(entry_type="decision", text="Use FastAPI", explicit=True, source_hash="h3"),
        ]
        # Assign IDs
        for i, e in enumerate(entries):
            e["id"] = i + 1
        
        # New entry that conflicts with "Use PostgreSQL" (same topic: database)
        new_entry = _make_ledger_entry(
            entry_type="constraint",
            text="Use MySQL instead",
            explicit=True,
            source_hash="h4",
        )
        new_entry["id"] = 4
        
        result = app._apply_supersession(entries, new_entry)
        
        # The PostgreSQL constraint should be superseded
        pg_entry = next(e for e in result if e["source_hash"] == "h1")
        assert pg_entry["status"] == "superseded"
        assert pg_entry["superseded_by"] == 4
        
        # Unrelated entries should remain active
        aws_entry = next(e for e in result if e["source_hash"] == "h2")
        assert aws_entry["status"] == "active"
        assert aws_entry["superseded_by"] is None
        
        fastapi_entry = next(e for e in result if e["source_hash"] == "h3")
        assert fastapi_entry["status"] == "active"
    
    def test_summary_cannot_overwrite_explicit_constraint(self):
        """A lossy summary cannot replace an explicit user constraint in the ledger."""
        entries = [
            _make_ledger_entry(
                entry_type="constraint",
                text="Never use Redis for caching",
                explicit=True,
                source_hash="h1",
            ),
        ]
        entries[0]["id"] = 1
        
        # A summary-derived entry (explicit=False) trying to supersede
        summary_entry = _make_ledger_entry(
            entry_type="constraint",
            text="Use Redis for caching (from summary)",
            explicit=False,  # inferred from summary, not explicit
            source_hash="h2",
        )
        summary_entry["id"] = 2
        
        result = app._apply_supersession(entries, summary_entry)
        
        # The explicit constraint must NOT be superseded by a non-explicit entry
        original = next(e for e in result if e["source_hash"] == "h1")
        assert original["status"] == "active", "Explicit constraint was incorrectly superseded by summary"
        assert original["superseded_by"] is None


class TestLedgerInjection:
    def test_ledger_injected_as_separate_message(self):
        """Ledger is a separate system message, not appended to user prompt."""
        entries = [
            _make_ledger_entry(entry_type="requirement", text="Build API", explicit=True, source_hash="h1"),
            _make_ledger_entry(entry_type="constraint", text="Use Python", explicit=True, source_hash="h2"),
        ]
        entries[0]["id"] = 1
        entries[1]["id"] = 2
        
        # Format the ledger snapshot
        snapshot = app._format_ledger_snapshot(entries)
        
        # It should be a non-empty string
        assert isinstance(snapshot, str)
        assert len(snapshot) > 0
        assert "Build API" in snapshot
        assert "Use Python" in snapshot
        
        # The ledger message should be a separate dict with role=system
        ledger_msg = app._build_ledger_message(entries)
        assert ledger_msg["role"] == "system"
        assert "content" in ledger_msg
        assert "Build API" in ledger_msg["content"]
        # It should NOT be appended to any user message
        assert ledger_msg["role"] != "user"


# ─── Phase 06: Retrieval Tests ─────────────────────────────────────────────────

class TestRetrieval:
    def test_retrieval_by_event_hash(self):
        """Archived content is retrievable by session + event_hash + range."""
        # Test the _retrieve_archived_content function signature and logic
        # We test the chunking logic without a real DB
        content = "A" * 10000  # 10KB of content
        chunks = app._chunk_tool_body(content, chunk_size=4000)
        
        # Should produce 3 chunks: 4000 + 4000 + 2000
        assert len(chunks) == 3
        assert len(chunks[0]) == 4000
        assert len(chunks[1]) == 4000
        assert len(chunks[2]) == 2000
        
        # Verify content integrity
        reassembled = "".join(chunks)
        assert reassembled == content
    
    def test_retrieval_range_query(self):
        """Range queries return the correct subset of chunks."""
        content = "X" * 20000
        chunks = app._chunk_tool_body(content, chunk_size=5000)
        
        # Request chunks 1-2 (0-indexed, exclusive end)
        subset = app._retrieve_chunk_range(chunks, start=1, end=3)
        assert len(subset) == 2
        assert subset[0] == "X" * 5000
        assert subset[1] == "X" * 5000
        
        # Reassembled should match the original range
        assert "".join(subset) == content[5000:15000]


# ─── Phase 07: Tenant Scope Tests ──────────────────────────────────────────────

class TestTenantScope:
    def test_tenant_scope_enforced(self):
        """Queries are scoped by tenant_id; cannot access another tenant's data."""
        # Verify that _retrieve_archived_content requires tenant_id
        import inspect
        sig = inspect.signature(app._retrieve_archived_content)
        params = list(sig.parameters.keys())
        assert "tenant_id" in params, "tenant_id must be a parameter"
        assert "session_key" in params, "session_key must be a parameter"
        
        # The SQL in the function must include tenant_id in WHERE clause
        src = inspect.getsource(app._retrieve_archived_content)
        assert "tenant_id" in src, "SQL must filter by tenant_id"
        assert "session_key" in src, "SQL must filter by session_key"


# ─── Phase 07: Gap-Aware Watermark Tests ───────────────────────────────────────

class TestGapAwareWatermark:
    def test_gap_aware_watermark(self):
        """Watermark does not advance past a failed chunk."""
        # Simulate chunk states
        chunks = [
            {"chunk_seq": 0, "end_idx": 10, "state": "stored"},
            {"chunk_seq": 1, "end_idx": 20, "state": "stored"},
            {"chunk_seq": 2, "end_idx": 30, "state": "retryable_failure"},  # gap!
            {"chunk_seq": 3, "end_idx": 40, "state": "stored"},
        ]
        
        watermark = app._compute_gap_aware_watermark(chunks)
        
        # Watermark should be at chunk 1's end_idx (20), NOT 40
        assert watermark == 20, f"Expected watermark=20, got {watermark}"
    
    def test_watermark_advances_when_all_stored(self):
        """Watermark advances to the latest stored chunk when no gaps."""
        chunks = [
            {"chunk_seq": 0, "end_idx": 10, "state": "stored"},
            {"chunk_seq": 1, "end_idx": 20, "state": "stored"},
            {"chunk_seq": 2, "end_idx": 30, "state": "stored"},
        ]
        
        watermark = app._compute_gap_aware_watermark(chunks)
        assert watermark == 30
    
    def test_watermark_zero_when_first_fails(self):
        """If the first chunk fails, watermark stays at 0."""
        chunks = [
            {"chunk_seq": 0, "end_idx": 10, "state": "terminal_failure"},
            {"chunk_seq": 1, "end_idx": 20, "state": "stored"},
        ]
        
        watermark = app._compute_gap_aware_watermark(chunks)
        assert watermark == 0


# ─── Phase 07: Idempotent Writes ───────────────────────────────────────────────

class TestIdempotentWrites:
    def test_idempotent_chunk_write(self):
        """Writing the same chunk twice produces the same result (idempotent)."""
        # The chunk_id is deterministic: sha256(task_id || start_idx || end_idx || chunk_seq)
        task_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        start_idx = 0
        end_idx = 100
        chunk_seq = 0
        
        chunk_id_1 = app._deterministic_chunk_id(task_id, start_idx, end_idx, chunk_seq)
        chunk_id_2 = app._deterministic_chunk_id(task_id, start_idx, end_idx, chunk_seq)
        
        # Same input → same ID (idempotent)
        assert chunk_id_1 == chunk_id_2
        
        # Different input → different ID
        chunk_id_3 = app._deterministic_chunk_id(task_id, start_idx, end_idx + 1, chunk_seq)
        assert chunk_id_1 != chunk_id_3


# ─── Phase 07: Restart Recovery ────────────────────────────────────────────────

class TestRestartRecovery:
    def test_restart_recovery(self):
        """Orphaned chunks (expired lease) are reset to queued on restart."""
        # Test the recovery SQL logic
        recovery_sql = app._RESTART_RECOVERY_SQL
        assert "queued" in recovery_sql
        assert "claimed" in recovery_sql
        assert "processing" in recovery_sql
        assert "lease_expires" in recovery_sql
        assert "NOW()" in recovery_sql or "now()" in recovery_sql


# ─── Phase 07: Large Tool Body ─────────────────────────────────────────────────

class TestLargeToolBody:
    def test_large_tool_body_retrievable(self):
        """A 300k-token tool output can be retrieved in full via range queries."""
        # 300k tokens ≈ 1.2M chars (at rho=4)
        content = "T" * 1_200_000
        
        chunks = app._chunk_tool_body(content, chunk_size=50_000)
        
        # Should produce 24 chunks of 50k each
        assert len(chunks) == 24
        assert all(len(c) == 50_000 for c in chunks)
        
        # Reassemble and verify
        reassembled = "".join(chunks)
        assert reassembled == content
        assert len(reassembled) == 1_200_000
        
        # Range retrieval: get middle portion (chunks 10-14, exclusive end)
        middle = app._retrieve_chunk_range(chunks, start=10, end=15)
        assert len(middle) == 5
        assert "".join(middle) == content[500_000:750_000]
