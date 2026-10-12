"""Archive retrieval API endpoint."""



async def retrieve_archived(
    session: str = "",
    event: str = "",
    start: int = 0,
    end: int = None,
    tenant: str = "",
):
    """Retrieve archived tool body content by session + event_hash + range.
    
    Tenant-scoped: requires tenant parameter matching the session's tenant.
    """
    from proxy.app import _session_tenant  # TODO(I4): replace with AppContext
    from proxy.archive.store import _retrieve_archived_content
    if not session or not event:
        return {"error": "session and event parameters required"}
    
    # Resolve tenant from session if not provided
    if not tenant:
        tenant = _session_tenant.get(session, "")
    if not tenant:
        return {"error": "tenant not resolved for session"}
    
    content = await _retrieve_archived_content(tenant, session, event, start, end)
    if not content:
        return {"error": "no archived content found", "session": session, "event": event}
    
    return {
        "session": session,
        "event": event,
        "start": start,
        "end": end if end else "all",
        "chars": len(content),
        "content": content,
    }



