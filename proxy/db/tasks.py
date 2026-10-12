"""Database task row management: ensure and resolve tasks."""
import logging

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


async def _ensure_task_row(meta: dict, tenant_id: str = ""):
    """Create proxy.tasks row (id=uuid5, session_id=goose id). Idempotent, race-safe."""
    # TODO(I4): replace with AppContext
    from proxy.app import pool
    if pool is None:
        return
    try:
        await pool.execute(
            "INSERT INTO proxy.tasks (id, session_id, name, session_type, working_dir, provider_name, tenant_id) "
            "VALUES ($1::uuid,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
            meta["uuid"], meta["id"], meta.get("name") or meta["id"],
            meta.get("session_type", ""), meta.get("working_dir", ""),
            meta.get("provider_name", ""), tenant_id,
        )
    except Exception as e:
        log.warning("task row init failed for %s: %s", meta.get("id"), e)
async def _resolve_task(task_ref: str, create: bool = False, tenant_id: str = ""):
    """Resolve or create a proxy task, enriching with Goose DB session metadata.

    Uses exact columns from Goose sessions DB: id, name, session_type, working_dir, provider_name.
    This ensures each Goose session gets its own properly-named proxy task.
    Phase 2: tenant_id is set on creation and used for tenant-scoped queries.
    """
    # TODO(I4): replace with AppContext
    from proxy.app import pool, _get_goose_session_info
    if not pool:
        return None
    # Phase 2: filter by tenant_id when available
    if tenant_id:
        row = await pool.fetchrow("SELECT id, name FROM proxy.tasks WHERE session_id = $1 AND tenant_id = $2", task_ref, tenant_id)
    else:
        row = await pool.fetchrow("SELECT id, name FROM proxy.tasks WHERE session_id = $1", task_ref)
    if row:
        # Update metadata if it's missing (lazy enrichment)
        if not row["name"]:
            info = await _get_goose_session_info(task_ref)
            if info:
                await pool.execute(
                    "UPDATE proxy.tasks SET name=$2, session_type=$3, working_dir=$4, provider_name=$5, updated_at=now() WHERE id=$1",
                    row["id"], info["name"], info["session_type"], info["working_dir"], info["provider_name"]
                )
            else:
                await pool.execute(
                    "UPDATE proxy.tasks SET name=$2, updated_at=now() WHERE id=$1",
                    row["id"], task_ref
                )
        # Phase 2: backfill tenant_id if missing
        if tenant_id:
            await pool.execute("UPDATE proxy.tasks SET tenant_id=$2 WHERE id=$1 AND tenant_id IS NULL", row["id"], tenant_id)
        return str(row["id"])
    if not create:
        return None
    # Fetch Goose DB metadata for enrichment
    info = await _get_goose_session_info(task_ref)
    name = info["name"] if info else task_ref
    session_type = info["session_type"] if info else ""
    working_dir = info["working_dir"] if info else ""
    provider_name = info["provider_name"] if info else ""
    if task_ref.startswith("noauth:"):
        import traceback as _tb
        log.warning("RESOLVE-TASK-CREATE task_ref=%s stack=%s",
                    task_ref, " | ".join(
                        "%s:%s" % (f.filename.split("/")[-1], f.lineno)
                        for f in _tb.extract_stack()[:-1][-8:]))
    await pool.execute(
        "INSERT INTO proxy.tasks (session_id, name, session_type, working_dir, provider_name, tenant_id) VALUES ($1,$2,$3,$4,$5,$6) "
        "ON CONFLICT (session_id) DO UPDATE SET name=COALESCE(EXCLUDED.name, proxy.tasks.name), "
        "session_type=COALESCE(EXCLUDED.session_type, proxy.tasks.session_type), "
        "working_dir=COALESCE(EXCLUDED.working_dir, proxy.tasks.working_dir), "
        "provider_name=COALESCE(EXCLUDED.provider_name, proxy.tasks.provider_name), "
        "tenant_id=COALESCE(EXCLUDED.tenant_id, proxy.tasks.tenant_id), updated_at=now()",
        task_ref, name, session_type, working_dir, provider_name, tenant_id,
    )
    row = await pool.fetchrow("SELECT id FROM proxy.tasks WHERE session_id = $1", task_ref)
    return str(row["id"])

