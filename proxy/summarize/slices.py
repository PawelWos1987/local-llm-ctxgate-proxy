"""Context slice enqueueing and session summary fetching."""
import os
import json
import asyncio


async def _enqueue_context_slices(task_uuid, session_key, chunks, start_idx, end_idx):
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    from proxy.db.events import _insert_event_row
    # Phase 5: Enqueue one events row + memory_jobs row per chunk for the worker.
    # Idempotent: keyed by (task_id, context_slice, slice_start, slice_end, chunk_idx).
    if not pool:
        return
    slice_chars = int(os.environ.get("CTXGATE_WORKER_SLICE_CHARS", "12000"))
    for ci, chunk in enumerate(chunks):
        c = chunk[:slice_chars] if len(chunk) > slice_chars else chunk
        dedupe_key = "ctx_slice|" + str(task_uuid) + "|" + str(start_idx) + "|" + str(end_idx) + "|" + str(ci)
        try:
            existing = await pool.fetchrow(
                "SELECT id FROM proxy.events WHERE task_id=$1 AND role=$2 AND meta->>'dedupe_key'=$3 LIMIT 1",
                task_uuid, "context_slice", dedupe_key
            )
            if existing:
                continue
            event_id, _ = await _insert_event_row(task_uuid, 'context_slice', c, json.dumps({'slice_start': start_idx, 'slice_end': end_idx, 'chunk_idx': ci, 'dedupe_key': dedupe_key}))
            await pool.execute(
                "INSERT INTO proxy.memory_jobs (task_id, event_id, status) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                task_uuid, event_id, "pending"
            )
            log.info("Enqueued context_slice: task=%s slice=[%d,%d) chunk=%d event=%s",
                     task_uuid, start_idx, end_idx, ci, event_id)
        except Exception as e:
            log.warning("Failed to enqueue context_slice (non-fatal): %s", e)






async def _fetch_session_summary(task_uuid, budget=1500, session_key: str = ""):
    """Fetch the most recent session summary for a task.

    Falls back to matching by session_key prefix (x_sid) if task_uuid has no summary,
    ensuring summaries are found even when task resolution changes.
    """
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    from proxy.token.exact import exact_count_string
    if not pool:
        return ""
    try:
        row = await pool.fetchrow("SELECT summary FROM proxy.session_summaries WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1", task_uuid)
        if not row and session_key:
            # Fallback: match by session_key prefix (the x_sid part)
            x_sid = session_key.split(":")[0] if ":" in session_key else session_key
            row = await pool.fetchrow(
                "SELECT ss.summary FROM proxy.session_summaries ss "
                "JOIN proxy.tasks t ON t.id = ss.task_id "
                "WHERE t.session_id = $1 ORDER BY ss.created_at DESC LIMIT 1",
                x_sid
            )
        if not row or not row["summary"]:
            return ""
        s = row["summary"].strip()
        t = len(s)  # char count for sizing
        if t <= budget:
            return s
        # Binary search for exact token budget fit using gigatoken
        if exact_count_string(s, VLLM_MODEL) <= budget:
            return s
        lo, hi = 0, len(s)
        while lo < hi:
            mid = (lo + hi) // 2
            if exact_count_string(s[:mid], VLLM_MODEL) <= budget:
                lo = mid + 1
            else:
                hi = mid
        return s[:lo]
    except Exception as e:
        log.warning("Session summary fetch failed: %s", e)
    return ""


# --- Constants ---
VLLM_URL = os.environ.get("CTXGATE_VLLM_URL", "http://127.0.0.1:29000/v1")
VLLM_MODEL = os.environ.get("CTXGATE_VLLM_MODEL", "Qwen3.8-27B")
# ─── Budget Configuration ───────────────────────────────────────────────────
# Primary source: Goose custom provider config (single source of truth).
# Fallback: env vars (CTXGATE_MAX_CONTEXT, CTXGATE_MAX_OUTPUT) → hardcoded defaults.
# All other budget values are derived to maintain the invariant:
#   MAX_INPUT + SAFETY_MARGIN + MAX_OUTPUT <= MAX_CONTEXT
#
# Derived:
#   MAX_OUTPUT    = max output budget
#   SAFETY_MARGIN = 5% of MAX_CONTEXT
#   MAX_INPUT     = MAX_CONTEXT - SAFETY_MARGIN - MAX_OUTPUT

_GOOSE_CONFIG_PATH = os.path.expanduser("~/.config/goose/custom_providers/custom_qwen.json")



