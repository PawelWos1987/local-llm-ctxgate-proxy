"""Session identity resolution: Goose header or content-fingerprint fallback.

P0: In-process LRU cache (maxsize=1000, TTL=300s) for the Goose session
metadata DB lookup to reduce pre-vLLM overhead on repeated requests.

P3: The only DB query in this module is a single SQLite SELECT in
_get_goose_session_info (one round trip). No separate sessions/tasks JOIN
is needed. The window-state load in persist.py is also a single query.
"""
import logging
import time
import uuid
from collections import OrderedDict
from typing import Optional

from fastapi import Request

from proxy.config import GOOSE_SESSION_UUID_NAMESPACE
from proxy.session.key import _fallback_fingerprint
from proxy.hotpath.session import _get_goose_session_info

log = logging.getLogger("ctxgate-proxy")

# ---------------------------------------------------------------------------
# P0: In-Process Session Identity Cache
# ---------------------------------------------------------------------------

_CACHE_MAX_SIZE: int = 1000
_CACHE_TTL_SEC: float = 300.0  # 5 minutes

# OrderedDict-based LRU cache.
# Key: session ID string (from header).
# Value: (resolved_info_dict, monotonic_timestamp)
# We only cache successful lookups (non-None). Misses are NOT cached so
# that a newly-created session becomes visible on the next request.
_session_info_cache: "OrderedDict[str, tuple[Optional[dict], float]]" = OrderedDict()


def _cache_get(key: str) -> Optional[dict]:
    """Retrieve a cached session-info dict, or None on miss/expiry.

    On hit: moves the entry to the most-recently-used position.
    On expiry: removes the entry and returns None (caller will re-query DB).
    """
    entry = _session_info_cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.monotonic() - ts > _CACHE_TTL_SEC:
        # TTL expired: evict and signal a miss
        del _session_info_cache[key]
        return None
    # LRU: mark as most recently used
    _session_info_cache.move_to_end(key)
    return value


def _cache_set(key: str, value: dict) -> None:
    """Store a resolved session-info dict in the LRU cache.

    Evicts the least-recently-used entry when the cache exceeds _CACHE_MAX_SIZE.
    """
    if key in _session_info_cache:
        _session_info_cache.move_to_end(key)
    _session_info_cache[key] = (value, time.monotonic())
    # Evict oldest entries beyond max size
    while len(_session_info_cache) > _CACHE_MAX_SIZE:
        _session_info_cache.popitem(last=False)


def invalidate_session_cache(session_id: str) -> None:
    """Invalidate a specific session's cached identity.

    Call this when a session is deleted or its metadata changes externally
    so that the next request re-queries the database.
    """
    if session_id in _session_info_cache:
        del _session_info_cache[session_id]
        log.debug("Session identity cache invalidated for %s", session_id)


def _cache_clear() -> None:
    """Clear the entire session identity cache (e.g. on shutdown)."""
    _session_info_cache.clear()


# ---------------------------------------------------------------------------
# Main resolution function
# ---------------------------------------------------------------------------

async def _resolve_session_identity(request: Request, messages: list, body: dict) -> dict:
    """Resolve session identity: Goose header or content-fingerprint fallback.

    Returns a dict with keys: id, uuid, name, session_type, working_dir,
    provider_name, log_label, client_cap, session_key.

    P0: The Goose session metadata DB lookup (_get_goose_session_info) is
    cached in-process (LRU, maxsize=1000, TTL=300s) to avoid repeated
    SQLite queries on hot paths. The UUID and session_key are computed
    fresh each call (cheap, no I/O).
    """
    _goose_id_header = (
        request.headers.get('agent-session-id', '')
        or request.headers.get('X-Session-ID', '')
        or ''
    ).strip()

    # Client cap only applies to the fallback path.
    _client_cap = None
    if not _goose_id_header:
        _cm = body.get("max_tokens")
        if isinstance(_cm, int) and _cm > 0:
            _client_cap = _cm

    if _goose_id_header:
        _goose_meta = {
            "id": _goose_id_header,
            "uuid": str(uuid.uuid5(
                GOOSE_SESSION_UUID_NAMESPACE, _goose_id_header)),
            "name": _goose_id_header,
            "session_type": "",
            "working_dir": "",
            "provider_name": "",
        }
        # P0: Try in-process cache first (avoids SQLite round-trip)
        _info = _cache_get(_goose_id_header)
        if _info is not None:
            log.debug("Session identity cache HIT for %s", _goose_id_header)
            _goose_meta.update({
                "name": _info.get("name") or _goose_id_header,
                "session_type": _info.get("session_type") or "",
                "working_dir": _info.get("working_dir") or "",
                "provider_name": _info.get("provider_name") or "",
            })
        else:
            log.debug("Session identity cache MISS for %s", _goose_id_header)
            try:
                _info = await _get_goose_session_info(_goose_id_header)
                if _info:
                    # Only cache successful lookups (not None/misses)
                    _cache_set(_goose_id_header, _info)
                    _goose_meta.update({
                        "name": _info.get("name") or _goose_id_header,
                        "session_type": _info.get("session_type") or "",
                        "working_dir": _info.get("working_dir") or "",
                        "provider_name": _info.get("provider_name") or "",
                    })
            except Exception as e:
                log.debug(
                    "goose session metadata enrichment failed (non-fatal): %s",
                    e)
        _log_label = "GOOSE-HEADER"
    else:
        _fb_fp = _fallback_fingerprint(messages)
        if not _fb_fp:
            _fb_fp = "empty"
        _fallback_id = f"fallback:{_fb_fp}"
        _goose_meta = {
            "id": _fallback_id,
            "uuid": str(uuid.uuid5(
                GOOSE_SESSION_UUID_NAMESPACE, _fallback_id)),
            "name": "fallback",
            "session_type": "fallback",
            "working_dir": "",
            "provider_name": "",
        }
        _log_label = "FALLBACK"

    session_key = f"goose:{_goose_meta['id']}"
    return {
        **_goose_meta,
        "log_label": _log_label,
        "client_cap": _client_cap,
        "session_key": session_key,
    }
