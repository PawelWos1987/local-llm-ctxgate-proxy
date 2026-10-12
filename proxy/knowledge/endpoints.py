"""Memory and knowledge endpoint handlers."""
import os
from fastapi import Request, Depends
from fastapi.responses import JSONResponse


async def memory_inject(request: Request):
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    from proxy.db.tasks import _resolve_task
    tenant_id = getattr(request.state, "tenant_id", "")
    body = await request.json()
    task_ref = body.get("task_id") or body.get("task_ref")
    content = body.get("content", "")
    if not task_ref:
        return JSONResponse({"error": "task_id required"}, status_code=400)
    task_uuid = await _resolve_task(task_ref, create=True, tenant_id=tenant_id)
    if task_uuid is None:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    await pool.execute(
        "INSERT INTO proxy.working_memory (task_id, content, tenant_id, updated_at) VALUES ($1, $2, $3, now()) "
        "ON CONFLICT (task_id) DO UPDATE SET content = $2, tenant_id = $3, updated_at = now()",
        task_uuid, content, tenant_id,
    )
    log.info("Injected working memory for task %s (%d chars)", task_ref, len(content))
    return {"status": "ok", "task_id": task_ref, "task_uuid": task_uuid}





async def memory_query(request: Request, task_ref: str):
    from proxy.app import pool  # TODO(I4): replace with AppContext
    from proxy.db.tasks import _resolve_task
    tenant_id = getattr(request.state, "tenant_id", "")
    task_uuid = await _resolve_task(task_ref, create=False, tenant_id=tenant_id)
    if task_uuid is None:
        return {"content": "", "updated_at": None}
    row = await pool.fetchrow(
        "SELECT content, updated_at FROM proxy.working_memory WHERE task_id = $1",
        task_uuid,
    )
    if not row:
        return {"content": "", "updated_at": None}
    return {"content": row["content"], "updated_at": str(row["updated_at"]) if row["updated_at"] else None}

# --- Web Dashboard ---


# --- Memory & utilization analytics API ---



async def api_memory(request: Request, session: str = "", limit: int = 50):
    """Per-session durable memories + working memory. Tenant-scoped."""
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    from proxy.db.tasks import _resolve_task
    if not pool or not session:
        return {"memories": [], "working_memory": "", "count": 0}
    tenant_id = getattr(request.state, "tenant_id", "")
    try:
        task_uuid = await _resolve_task(session, create=False, tenant_id=tenant_id)
        if task_uuid is None:
            return {"memories": [], "working_memory": "", "count": 0}
        rows = await pool.fetch(
            "SELECT key, value, category, importance, active, status, model_name, "
            "created_at, updated_at, source_event_id FROM proxy.memories "
            "WHERE task_id = $1 ORDER BY updated_at DESC LIMIT $2",
            task_uuid, limit,
        )
        memories = [
            {
                "key": r["key"], "value": r["value"], "category": r["category"],
                "importance": r["importance"], "active": r["active"], "status": r["status"],
                "model_name": r["model_name"],
                "created_at": str(r["created_at"]) if r["created_at"] else None,
                "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
                "source_event_id": str(r["source_event_id"]) if r["source_event_id"] else None,
            }
            for r in rows
        ]
        wrow = await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id = $1", task_uuid)
        wm = (wrow["content"] if wrow and wrow["content"] else "")
        return {"memories": memories, "working_memory": wm, "count": len(memories)}
    except Exception as e:
        log.warning("api/memory failed: %s", e)
        return {"memories": [], "working_memory": "", "count": 0, "error": str(e)}







async def api_memory_analytics(request: Request):
    """Full memory/knowledge/worker utilization analytics for the dashboard."""
    from proxy.app import injection_metrics, pool, _pct_str  # TODO(I4): replace with AppContext
    from proxy.config.constants import MISTRAL_MODEL, MISTRAL_URL
    from proxy.config.env import _env_int
    result = {}
    inj = injection_metrics
    total_reqs = inj.get("total_requests", 0)
    def _rate(n):
        return (n / total_reqs) if total_reqs else 0.0
    result["injection"] = {
        "task_memory_injections": inj.get("task_memory_injections", 0),
        "task_memory_tokens": inj.get("task_memory_tokens", 0),
        "knowledge_injections": inj.get("knowledge_injections", 0),
        "knowledge_tokens": inj.get("knowledge_tokens", 0),
        "working_memory_injections": inj.get("working_memory_injections", 0),
        "working_memory_tokens": inj.get("working_memory_tokens", 0),
        "total_requests": total_reqs,
        "task_memory_rate": round(_rate(inj.get("task_memory_injections", 0)), 4),
        "knowledge_rate": round(_rate(inj.get("knowledge_injections", 0)), 4),
        "wm_rate": round(_rate(inj.get("working_memory_injections", 0)), 4),
        "last_injected_task_memory": inj.get("last_injected_task_memory", ""),
        "last_injected_knowledge": inj.get("last_injected_knowledge", ""),
        "events": inj.get("events", []),
    }

    # Memories
    try:
        if pool:
            mem_total = await pool.fetchval("SELECT COUNT(*) FROM proxy.memories")
            mem_active = await pool.fetchval("SELECT COUNT(*) FROM proxy.memories WHERE active = true")
            mem_super = await pool.fetchval("SELECT COUNT(*) FROM proxy.memories WHERE active = false")
            by_cat = await pool.fetch("SELECT category, COUNT(*) c FROM proxy.memories WHERE active = true GROUP BY category ORDER BY c DESC")
            by_imp = await pool.fetch("SELECT importance, COUNT(*) c FROM proxy.memories WHERE active = true GROUP BY importance ORDER BY importance DESC")
            by_model = await pool.fetch("SELECT model_name, COUNT(*) c FROM proxy.memories GROUP BY model_name ORDER BY c DESC")
            recent = await pool.fetch("SELECT key, value, category, importance, updated_at, model_name FROM proxy.memories WHERE active = true ORDER BY updated_at DESC LIMIT 15")
            result["memories"] = {
                "total": mem_total, "active": mem_active, "superseded": mem_super,
                "by_category": {r["category"]: r["c"] for r in by_cat},
                "by_importance": {int(r["importance"]): r["c"] for r in by_imp},
                "by_model": {r["model_name"]: r["c"] for r in by_model},
                "recent": [
                    {"key": r["key"], "value": r["value"], "category": r["category"],
                     "importance": r["importance"], "model_name": r["model_name"],
                     "updated_at": str(r["updated_at"]) if r["updated_at"] else None}
                    for r in recent
                ],
            }
        else:
            result["memories"] = {"total": 0, "active": 0, "superseded": 0, "by_category": {}, "by_importance": {}, "by_model": {}, "recent": []}
    except Exception as e:
        result["memories"] = {"total": 0, "active": 0, "superseded": 0, "by_category": {}, "by_importance": {}, "by_model": {}, "recent": [], "error": str(e)}

    # Jobs
    try:
        if pool:
            j_total = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs")
            j_done = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE status = 'done'")
            j_failed = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE status = 'failed'")
            j_pending = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE status IN ('pending','running')")
            j_avg = await pool.fetchval("SELECT COALESCE(AVG(EXTRACT(epoch FROM completed_at - started_at)) * 1000, 0) FROM proxy.memory_jobs WHERE completed_at IS NOT NULL AND started_at IS NOT NULL")
            result["jobs"] = {
                "total": j_total, "done": j_done, "failed": j_failed, "pending": j_pending,
                "avg_ms": round(j_avg or 0, 1),
                "success_rate": round(j_done / j_total, 4) if j_total else 0.0,
            }
            by_hour = await pool.fetch(
                "SELECT to_char(date_trunc('hour', completed_at), 'HH24') AS hour, "
                "COUNT(*) FILTER (WHERE status = 'done') AS done, "
                "COUNT(*) FILTER (WHERE status = 'failed') AS failed "
                "FROM proxy.memory_jobs WHERE completed_at >= now() - interval '24 hours' "
                "GROUP BY 1 ORDER BY 1"
            )
            result["jobs_by_hour"] = [{"hour": r["hour"], "done": r["done"], "failed": r["failed"]} for r in by_hour]
        else:
            result["jobs"] = {"total": 0, "done": 0, "failed": 0, "pending": 0, "avg_ms": 0, "success_rate": 0.0}
            result["jobs_by_hour"] = []
    except Exception as e:
        result["jobs"] = {"total": 0, "done": 0, "failed": 0, "pending": 0, "avg_ms": 0, "success_rate": 0.0, "error": str(e)}
        result["jobs_by_hour"] = []

    # Knowledge
    try:
        if pool:
            k_total = await pool.fetchval("SELECT COUNT(*) FROM proxy.knowledge")
            k_active = await pool.fetchval("SELECT COUNT(*) FROM proxy.knowledge WHERE active = true")
            k_dom = await pool.fetch("SELECT domain, COUNT(*) c FROM proxy.knowledge WHERE active = true GROUP BY domain ORDER BY c DESC")
            k_recent = await pool.fetch("SELECT key, value, importance, domain, updated_at FROM proxy.knowledge WHERE active = true ORDER BY updated_at DESC LIMIT 10")
            result["knowledge"] = {
                "total": k_total, "active": k_active,
                "by_domain": {r["domain"]: r["c"] for r in k_dom},
                "recent": [
                    {"key": r["key"], "value": r["value"], "importance": r["importance"],
                     "domain": r["domain"], "updated_at": str(r["updated_at"]) if r["updated_at"] else None}
                    for r in k_recent
                ],
            }
        else:
            result["knowledge"] = {"total": 0, "active": 0, "by_domain": {}, "recent": []}
    except Exception as e:
        result["knowledge"] = {"total": 0, "active": 0, "by_domain": {}, "recent": [], "error": str(e)}

    # Sessions (per-session injection breakdown)
    result["sessions"] = inj.get("sessions", {})

    # Worker
    last_job = None
    jobs_today = 0
    try:
        if pool:
            lj = await pool.fetchrow("SELECT completed_at FROM proxy.memory_jobs WHERE status = 'done' AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1")
            if lj and lj["completed_at"]:
                last_job = str(lj["completed_at"])
            jobs_today = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE status = 'done' AND completed_at >= date_trunc('day', now())")
    except Exception:
        pass
    result["worker"] = {
        "model": MISTRAL_MODEL,
        "lm_url": MISTRAL_URL + "/chat/completions",
        "poll": float(os.environ.get("CTXGATE_WORKER_POLL", "2.0")),
        "max_attempts": _env_int("CTXGATE_WORKER_MAX_ATTEMPTS", 3),
        "outage_ttl": float(os.environ.get("CTXGATE_WORKER_OUTAGE_TTL", "1800")),
        "last_job_completed_at": last_job,
        "jobs_done_today": jobs_today,
    }

    # Utilization summary (human-readable)
    n = total_reqs
    tm_i = inj.get("task_memory_injections", 0)
    kn_i = inj.get("knowledge_injections", 0)
    wm_i = inj.get("working_memory_injections", 0)
    mem_total = result.get("memories", {}).get("total", 0)
    j_done = result.get("jobs", {}).get("done", 0)
    j_avg = result.get("jobs", {}).get("avg_ms", 0)
    k_total = result.get("knowledge", {}).get("total", 0)
    result["utilization_summary"] = [
        f"Task memory injected in {_pct_str(tm_i, n)} of {n} requests ({tm_i} times)",
        f"Shared knowledge injected in {_pct_str(kn_i, n)} of {n} requests ({kn_i} times)",
        f"Working memory injected in {_pct_str(wm_i, n)} of {n} requests ({wm_i} times)",
        f"4B worker produced {mem_total} memories from {j_done} jobs (avg {(j_avg / 1000):.1f} s)",
        f"Knowledge sharing: {k_total} items, used in {_pct_str(kn_i, n)} of requests",
    ]
    return result




