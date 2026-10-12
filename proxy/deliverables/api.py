"""Deliverable CRUD API endpoints."""
from fastapi import Request, Depends
from fastapi.responses import JSONResponse



async def create_deliverable(request: Request):
    """Register a new deliverable. Called by the agent when a task produces a document."""
    from proxy.app import log, pool, _get_goose_session_id, _get_goose_session_info  # TODO(I4): replace with AppContext
    if not pool:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    tenant_id = getattr(request.state, "tenant_id", "")
    try:
        body = await request.json()
        name = body.get("name", "unnamed")
        session_type = body.get("session_type", "")
        working_dir = body.get("working_dir", "")
        provider_name = body.get("provider_name", "")
        summary = body.get("summary", "")
        document_path = body.get("document_path", "")

        # Auto-enrichment: fill missing fields from Goose sessions DB
        if not session_type or not working_dir or not provider_name:
            x_sid = request.headers.get("X-Session-ID", "")
            if not x_sid:
                x_sid = await _get_goose_session_id()
            if x_sid:
                info = await _get_goose_session_info(x_sid)
                if info:
                    if not session_type:
                        session_type = info.get("session_type", "") or "goose"
                    if not working_dir:
                        working_dir = info.get("working_dir", "")
                    if not provider_name:
                        provider_name = info.get("provider_name", "")
        if not session_type:
            session_type = "goose"

        row = await pool.fetchrow(
            """INSERT INTO proxy.deliverables
               (name, session_type, working_dir, provider_name, summary, document_path, tenant_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id, name, session_type, working_dir, provider_name, summary, document_path, created_at""",
            name, session_type, working_dir, provider_name, summary, document_path, tenant_id
        )
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "session_type": row["session_type"],
            "working_dir": row["working_dir"],
            "provider_name": row["provider_name"],
            "summary": row["summary"],
            "document_path": row["document_path"],
            "created_at": row["created_at"].isoformat()
        }
    except Exception as e:
        log.error("deliverable create error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)





async def list_deliverables(request: Request, limit: int = 50):
    """List recent deliverables for the dashboard. Tenant-scoped."""
    from proxy.app import log, pool, _get_goose_session_id, _get_goose_session_info  # TODO(I4): replace with AppContext
    if not pool:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    tenant_id = getattr(request.state, "tenant_id", "")
    try:
        if tenant_id:
            rows = await pool.fetch(
                """SELECT id, name, session_type, working_dir, provider_name, summary, document_path, created_at, updated_at
                   FROM proxy.deliverables WHERE tenant_id = $1
                   ORDER BY created_at DESC
                   LIMIT $2""",
                tenant_id, limit
            )
        else:
            rows = await pool.fetch(
                """SELECT id, name, session_type, working_dir, provider_name, summary, document_path, created_at, updated_at
                   FROM proxy.deliverables
                   ORDER BY created_at DESC
                   LIMIT $1""",
                limit
            )
        return {
            "data": [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "session_type": r["session_type"],
                    "working_dir": r["working_dir"],
                    "provider_name": r["provider_name"],
                    "summary": r["summary"],
                    "document_path": r["document_path"],
                    "created_at": r["created_at"].isoformat(),
                    "updated_at": r["updated_at"].isoformat()
                }
                for r in rows
            ]
        }
    except Exception as e:
        log.error("deliverable list error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)





async def update_deliverable(id: str, request: Request):
    """Update a deliverable's summary, name, or document_path."""
    from proxy.app import log, pool, _get_goose_session_id, _get_goose_session_info  # TODO(I4): replace with AppContext
    if not pool:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    tenant_id = getattr(request.state, "tenant_id", "")
    try:
        body = await request.json()
        sets = []
        params = []
        param_idx = 1
        if body.get("summary") is not None:
            sets.append("summary=$" + str(param_idx))
            params.append(body["summary"])
            param_idx += 1
        if body.get("name") is not None:
            sets.append("name=$" + str(param_idx))
            params.append(body["name"])
            param_idx += 1
        if body.get("document_path") is not None:
            sets.append("document_path=$" + str(param_idx))
            params.append(body["document_path"])
            param_idx += 1
        if not sets:
            return JSONResponse({"error": "No updatable fields provided"}, status_code=400)
        sets.append("updated_at=now()")
        # Phase 2: tenant-scoped update
        if tenant_id:
            params.append(tenant_id)
            row = await pool.fetchrow(
                "UPDATE proxy.deliverables SET " + ", ".join(sets) +
                " WHERE id=$" + str(param_idx) + " AND tenant_id=$" + str(param_idx + 1) +
                " RETURNING id, name, session_type, working_dir, provider_name, summary, document_path, created_at, updated_at",
                *params
            )
        else:
            params.append(id)
            row = await pool.fetchrow(
                "UPDATE proxy.deliverables SET " + ", ".join(sets) + " WHERE id=$" + str(param_idx) +
                " RETURNING id, name, session_type, working_dir, provider_name, summary, document_path, created_at, updated_at",
                *params
            )
        if not row:
            return JSONResponse({"error": "Deliverable not found"}, status_code=404)
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "session_type": row["session_type"],
            "working_dir": row["working_dir"],
            "provider_name": row["provider_name"],
            "summary": row["summary"],
            "document_path": row["document_path"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat()
        }
    except Exception as e:
        log.error("deliverable update error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


