"""Ledger snapshot formatting and prefetching."""
import os



def _format_ledger_snapshot(entries: list) -> str:
    """Format active ledger entries as a compact, structured snapshot string."""
    active = [e for e in entries if e.get("status") == "active"]
    if not active:
        return ""
    
    type_markers = {
        "requirement": "R", "constraint": "C", "decision": "D",
        "rejected_approach": "X", "completed_work": "W",
        "test_result": "T", "failure": "F", "artifact": "A", "todo": "!",
    }
    
    lines = ["[LEDGER] (%d active entries)" % len(active)]
    for e in active:
        marker = type_markers.get(e.get("entry_type", ""), "?")
        text = e.get("text", "")
        if len(text) > 200:
            text = text[:197] + "..."
        explicit_tag = "" if e.get("explicit", False) else " (inferred)"
        lines.append("[%s] %s: %s%s" % (marker, e.get("entry_type", "?"), text, explicit_tag))
    
    return chr(10).join(lines)





def _build_ledger_message(entries: list) -> dict:
    """Build a separate system message containing the ledger snapshot."""
    snapshot = _format_ledger_snapshot(entries)
    if not snapshot:
        return None
    return {
        "role": "system",
        "content": snapshot,
    }





async def _prefetch_ledger(task_uuid: str, session_key: str = "", tenant_id: str = "") -> None:
    """Prefetch ledger entries into the in-memory cache."""
    from proxy.app import _ledger_cache  # TODO(I4): replace with AppContext
    from proxy.ledger.persist import _fetch_active_ledger
    if not session_key and not task_uuid:
        return
    entries = await _fetch_active_ledger(task_uuid, session_key, tenant_id)
    if entries:
        cache_key = (tenant_id, session_key) if session_key else (tenant_id, task_uuid)
        _ledger_cache[cache_key] = entries

# ─── Phase 07: Tool Body Archive + Gap-Aware Watermark ─────────────────────────

TOOL_BODY_CHUNK_CHARS = int(os.environ.get("CTXGATE_TOOL_BODY_CHUNK_CHARS", "50000"))



