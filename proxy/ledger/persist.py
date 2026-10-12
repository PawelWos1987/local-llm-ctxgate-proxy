"""Ledger entry persistence to database."""



async def _persist_ledger_entries(entries: list, task_uuid):
    """Insert ledger entries with ON CONFLICT DO NOTHING (idempotent)."""
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    if not pool or not entries:
        return 0
    inserted = 0
    for e in entries:
        try:
            await pool.execute(
                "INSERT INTO proxy.session_ledger "
                "(task_id, kind, title, detail, evidence, source, slice_start, slice_end, dedupe_hash) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                "ON CONFLICT (task_id, dedupe_hash) DO NOTHING",
                task_uuid, e["kind"], e["title"], e["detail"], e.get("evidence", ""),
                e.get("source", ""), e.get("slice_start", 0), e.get("slice_end", 0), e["dedupe_hash"]
            )
            inserted += 1
        except Exception as ex:
            log.debug("Ledger insert failed: %s", ex)
    return inserted

# ─── Phase 06: Structured Ledger + Supersession + Retrieval ───────────────────

_LEDGER_TYPES = {
    "requirement", "constraint", "decision", "rejected_approach",
    "completed_work", "test_result", "failure", "artifact", "todo",
}
_LEDGER_STATUSES = {"active", "completed", "superseded", "rejected", "uncertain"}

# Topic keywords for conflict detection in supersession
_CONFLICT_TOPICS = {
    "database": ["postgres", "mysql", "sqlite", "redis", "mongo", "db", "database"],
    "language": ["python", "java", "rust", "go", "typescript", "javascript"],
    "framework": ["fastapi", "flask", "django", "express", "react", "vue"],
    "deployment": ["aws", "gcp", "azure", "docker", "kubernetes", "deploy"],
    "caching": ["redis", "memcached", "cache", "caching"],
    "testing": ["pytest", "unittest", "test", "testing"],
    "architecture": ["microservice", "monolith", "event-driven", "architecture"],
}





async def _fetch_active_ledger(task_uuid: str, session_key: str = "", tenant_id: str = "") -> list:
    """Fetch active ledger entries for a session/task. Tenant-scoped."""
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    if not pool:
        return []
    try:
        rows = await pool.fetch(
            "SELECT id, entry_type, status, source_event_id, source_hash, "
            "source_span, text, explicit, superseded_by "
            "FROM proxy.ledger_entries "
            "WHERE tenant_id=$1 AND session_key=$2 AND status='active' "
            "ORDER BY id",
            tenant_id, session_key,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("_fetch_active_ledger failed: %s", e)
        return []



