"""FastAPI auth middleware."""
import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from proxy.config import CTXGATE_ALLOW_NO_AUTH
from proxy.resilience.rate_limit import _TokenBucket



async def require_auth(request: Request):
    """FastAPI dependency: validates Authorization: Bearer <key>.
    Attaches tenant_id and is_admin to request.state.
    Raises 401 for missing/invalid keys, 403 for tenant keys on admin routes."""
    from proxy.auth.keys import _validate_key
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        key = auth_header[7:]
    else:
        key = ""
    is_valid, is_admin, tenant_id = _validate_key(key)
    if not is_valid:
        if CTXGATE_ALLOW_NO_AUTH:
            # Allow no-auth mode: assign a synthetic tenant
            request.state.tenant_id = "noauth"
            request.state.is_admin = False
            return
        return JSONResponse({"error": {"message": "Unauthorized"}}, status_code=401)
    request.state.tenant_id = tenant_id
    request.state.is_admin = is_admin





def require_admin(request: Request):
    """FastAPI dependency: requires admin key. Raises 403 for tenant keys."""
    from proxy.auth.keys import _validate_key
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        key = auth_header[7:]
    else:
        key = ""
    is_valid, is_admin, tenant_id = _validate_key(key)
    if not is_valid:
        if CTXGATE_ALLOW_NO_AUTH:
            request.state.tenant_id = "noauth"
            request.state.is_admin = False
            return
        return JSONResponse({"error": {"message": "Unauthorized"}}, status_code=401)
    if not is_admin:
        return JSONResponse({"error": {"message": "Admin access required"}}, status_code=403)
    request.state.tenant_id = tenant_id
    request.state.is_admin = True

# Per-tenant rate limiters and semaphores (created on demand)
_tenant_rate_limiters: dict[str, _TokenBucket] = {}
_tenant_semaphores: dict[str, asyncio.Semaphore] = {}




