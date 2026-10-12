"""Knowledge CRUD API endpoints."""
from fastapi import Request, Depends
from fastapi.responses import JSONResponse


async def knowledge_create(request: Request):
    """Create or update a knowledge item. Tenant-scoped."""
    from proxy.app import pool  # TODO(I4): replace with AppContext
    if not pool:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    tenant_id = getattr(request.state, "tenant_id", "")
    body = await request.json()
    domain = body.get("domain", "general")
    key = body.get("key", "")
    value = body.get("value", "")
    importance = body.get("importance", 5)
    source_session = body.get("source_session", "")
    if not key or not value:
        return JSONResponse({"error": "key and value required"}, status_code=400)
    await pool.execute(
        "INSERT INTO proxy.knowledge (domain, key, value, importance, source_session, tenant_id, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, now()) "
        "ON CONFLICT (tenant_id, domain, key) WHERE active = true "
        "DO UPDATE SET value = EXCLUDED.value, importance = GREATEST(EXCLUDED.importance, proxy.knowledge.importance), "
        "source_session = EXCLUDED.source_session, updated_at = now()",
        domain, key, value, importance, source_session, tenant_id
    )
    return {"status": "ok", "domain": domain, "key": key}





async def knowledge_search(request: Request, q: str = "", domain: str = "", limit: int = 20):
    """Search knowledge items by keyword and/or domain. Tenant-scoped."""
    from proxy.app import pool  # TODO(I4): replace with AppContext
    if not pool:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    tenant_id = getattr(request.state, "tenant_id", "")
    query_terms = q.split() if q else []
    rows = []
    try:
        _tf = "AND tenant_id = $1 " if tenant_id else ""
        _ti = 1
        if domain and query_terms:
            if tenant_id:
                rows = await pool.fetch(
                    "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                    "FROM proxy.knowledge WHERE active = true AND tenant_id = $1 AND domain = $2 "
                    "AND (key ILIKE ANY($3) OR value ILIKE ANY($3)) "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $4",
                    tenant_id, domain, [f"%{t}%" for t in query_terms], limit
                )
            else:
                rows = await pool.fetch(
                    "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                    "FROM proxy.knowledge WHERE active = true AND domain = $1 "
                    "AND (key ILIKE ANY($2) OR value ILIKE ANY($2)) "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $3",
                    domain, [f"%{t}%" for t in query_terms], limit
                )
        elif domain:
            if tenant_id:
                rows = await pool.fetch(
                    "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                    "FROM proxy.knowledge WHERE active = true AND tenant_id = $1 AND domain = $2 "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $3",
                    tenant_id, domain, limit
                )
            else:
                rows = await pool.fetch(
                    "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                    "FROM proxy.knowledge WHERE active = true AND domain = $1 "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $2",
                    domain, limit
                )
        elif query_terms:
            if tenant_id:
                rows = await pool.fetch(
                    "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                    "FROM proxy.knowledge WHERE active = true AND tenant_id = $1 "
                    "AND (key ILIKE ANY($2) OR value ILIKE ANY($2)) "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $3",
                    tenant_id, [f"%{t}%" for t in query_terms], limit
                )
            else:
                rows = await pool.fetch(
                    "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                    "FROM proxy.knowledge WHERE active = true "
                    "AND (key ILIKE ANY($1) OR value ILIKE ANY($1)) "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $2",
                    [f"%{t}%" for t in query_terms], limit
                )
        else:
            if tenant_id:
                rows = await pool.fetch(
                    "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                    "FROM proxy.knowledge WHERE active = true AND tenant_id = $1 "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $2", tenant_id, limit
                )
            else:
                rows = await pool.fetch(
                    "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                    "FROM proxy.knowledge WHERE active = true "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $1", limit
                )
    except Exception as e:
        return {"count": 0, "items": [], "error": str(e)}
    return {
        "count": len(rows),
        "items": [
            {"key": r["key"], "value": r["value"], "importance": r["importance"],
             "domain": r["domain"], "source_session": r["source_session"] or "",
             "created_at": str(r["created_at"]), "updated_at": str(r["updated_at"])}
            for r in rows
        ]
    }





async def knowledge_stats(request: Request):
    """Knowledge store statistics. Tenant-scoped."""
    from proxy.app import pool  # TODO(I4): replace with AppContext
    if not pool:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    tenant_id = getattr(request.state, "tenant_id", "")
    if tenant_id:
        total = await pool.fetchval("SELECT COUNT(*) FROM proxy.knowledge WHERE active = true AND tenant_id = $1", tenant_id)
        by_domain = await pool.fetch(
            "SELECT domain, COUNT(*) as cnt FROM proxy.knowledge WHERE active = true AND tenant_id = $1 GROUP BY domain ORDER BY cnt DESC",
            tenant_id
        )
    else:
        total = await pool.fetchval("SELECT COUNT(*) FROM proxy.knowledge WHERE active = true")
        by_domain = await pool.fetch(
            "SELECT domain, COUNT(*) as cnt FROM proxy.knowledge WHERE active = true GROUP BY domain ORDER BY cnt DESC"
        )
    return {"total": total, "by_domain": [{"domain": r["domain"], "count": r["cnt"]} for r in by_domain]}



