"""Window persistence: save/load/cleanup window state in database.

P1: In-process LRU cache (maxsize=500, TTL=300s) for window state to
eliminate the DB round-trip on every build_context call.

P4: Non-blocking persist — the in-memory cache is updated synchronously,
and the DB write is scheduled as a background asyncio task. A periodic
flush (30s) and a graceful-shutdown flush ensure durability.
"""
import asyncio
import copy
import logging
import time
from collections import OrderedDict
from typing import Optional

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext
# Module-level state for persist cooldown
_window_persist_enabled: bool = True
_window_persist_warned: bool = False
_window_persist_cooldown_until: float = 0.0

# ---------------------------------------------------------------------------
# P1: In-Process Window State Cache
# ---------------------------------------------------------------------------

_WINDOW_CACHE_MAX_SIZE: int = 500
_WINDOW_CACHE_TTL_SEC: float = 300.0  # 5 minutes

# OrderedDict-based LRU cache.
# Key: session_key string.
# Value: (window_state_dict, monotonic_timestamp)
# We only cache non-None states. None (no window yet) is NOT cached so
# that a newly-created window becomes visible on the next request.
_window_cache: "OrderedDict[str, tuple[dict, float]]" = OrderedDict()


def _window_cache_get(key: str) -> Optional[dict]:
    """Retrieve a cached window-state dict, or None on miss/expiry.

    On hit: moves the entry to the most-recently-used position and
    returns a deep copy to prevent aliasing/mutation bugs.
    On expiry: removes the entry and returns None (caller will re-query DB).
    """
    entry = _window_cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.monotonic() - ts > _WINDOW_CACHE_TTL_SEC:
        # TTL expired: evict and signal a miss
        del _window_cache[key]
        return None
    # LRU: move to most-recently-used
    _window_cache.move_to_end(key)
    return copy.deepcopy(value)


def _window_cache_put(key: str, value: dict) -> None:
    """Store a window-state dict in the cache.

    Evicts the least-recently-used entry if the cache exceeds max size.
    Stores a deep copy to prevent external mutation of the cached value.
    """
    if key in _window_cache:
        _window_cache.move_to_end(key)
    _window_cache[key] = (copy.deepcopy(value), time.monotonic())
    # Evict LRU entries beyond max size
    while len(_window_cache) > _WINDOW_CACHE_MAX_SIZE:
        _window_cache.popitem(last=False)


def invalidate_window_cache(session_key: str) -> None:
    """Remove a specific session's window state from the in-process cache.

    Call this after external modifications (e.g. manual DB edits,
    session deletion) to force a fresh DB load on the next request.
    """
    _window_cache.pop(session_key, None)


def clear_window_cache() -> None:
    """Clear the entire in-process window state cache.

    Primarily for testing.
    """
    _window_cache.clear()


def _window_cache_stats() -> dict:
    """Return cache statistics for observability."""
    return {
        "size": len(_window_cache),
        "max_size": _WINDOW_CACHE_MAX_SIZE,
        "ttl_sec": _WINDOW_CACHE_TTL_SEC,
    }


# ---------------------------------------------------------------------------
# P4: Async Non-Blocking Persist
# ---------------------------------------------------------------------------

# Tracks in-flight background DB writes
_pending_persists: set[asyncio.Task] = set()

# Counter for failed background persists (resettable)
_persist_failure_count: int = 0


def _persist_failure_count_get() -> int:
    """Return the current count of failed background persists."""
    return _persist_failure_count


def _persist_failure_count_reset() -> None:
    """Reset the persist failure counter."""
    global _persist_failure_count
    _persist_failure_count = 0


async def _flush_pending_persists() -> None:
    """Await all in-flight background persist tasks.

    Called on graceful shutdown and periodically (every 30s) as a safety net.
    """
    global _persist_failure_count
    if not _pending_persists:
        return
    tasks = list(_pending_persists)
    _pending_persists.clear()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    log.debug("Flushed %d pending window persists", len(tasks))


async def _periodic_flush_loop(interval: float = 30.0) -> None:
    """Periodically flush pending persists as a safety net.

    Runs as a background task for the lifetime of the process.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await _flush_pending_persists()
        except Exception:
            log.exception("Periodic window persist flush failed")


# ---------------------------------------------------------------------------
# Core persist / load
# ---------------------------------------------------------------------------

async def _window_persist(session_key: str, ws: dict) -> None:
    """Upsert the window row. Feature-detects the table; a failure never fails a
    request (skips persistence for 60s after an error, warns once).

    P1: Updates the in-process cache synchronously before scheduling the DB write.
    P4: The DB write is scheduled as a background asyncio task (non-blocking).
    """
    global _window_persist_enabled, _window_persist_warned, _window_persist_cooldown_until
    if _window_persist_enabled is False:
        return

    # P1: Update in-memory cache immediately (synchronous)
    if ws is not None:
        _window_cache_put(session_key, ws)

    # TODO(I4): replace with AppContext
    from proxy.app import pool, metrics
    if not pool:
        return
    if time.monotonic() < _window_persist_cooldown_until:
        return

    # P4: Schedule the DB write as a background task (non-blocking)
    task = asyncio.create_task(_window_persist_db(session_key, ws))
    _pending_persists.add(task)
    task.add_done_callback(_pending_persists.discard)


async def _window_persist_db(session_key: str, ws: dict) -> None:
    """Perform the actual DB upsert for window state.

    Runs as a background task. Failures are logged but never propagated
    to the request path. The next request will re-persist.
    """
    global _window_persist_enabled, _window_persist_warned, _window_persist_cooldown_until
    global _persist_failure_count

    from proxy.app import pool, metrics
    if not pool:
        return

    try:
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO \"window\" (session_key, state, updated_at) "
                    "VALUES ($1, $2, now()) "
                    "ON CONFLICT (session_key) DO UPDATE SET state = $2, updated_at = now()",
                    session_key,
                    _state_to_json(ws),
                )
            except Exception as e:
                if "does not exist" in str(e):
                    if _window_persist_enabled is None:
                        _window_persist_enabled = False
                        _window_persist_warned = True
                        log.warning("Window persistence disabled: table 'window' not found")
                    return
                if _window_persist_enabled is None:
                    _window_persist_enabled = True
                _window_persist_cooldown_until = time.monotonic() + 60
                if not _window_persist_warned:
                    _window_persist_warned = True
                    log.warning("Window persist failed (cooldown 60s): %s", e)
                raise
        # Success: clear cooldown
        _window_persist_cooldown_until = 0.0
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # P4: Track failures, do NOT retry (next request will re-persist)
        _persist_failure_count += 1
        log.warning("Background window persist failed for %s: %s", session_key, e)


def _state_to_json(ws: dict) -> str:
    """Serialize window state dict to JSON string for DB storage."""
    import json
    return json.dumps(ws)


async def _window_load(session_key: str) -> Optional[dict]:
    """Load window state: cache-first, DB fallback.

    P1: Checks the in-process LRU cache first. On hit, returns a deep copy.
    On miss, loads from the DB and populates the cache.

    Feature-detects the table; returns None if the table doesn't exist
    or no row is found.
    """
    # P1: Check in-process cache first
    cached = _window_cache_get(session_key)
    if cached is not None:
        return cached

    # TODO(I4): replace with AppContext
    from proxy.app import pool
    if not pool:
        return None

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state FROM \"window\" WHERE session_key = $1",
                session_key,
            )
    except Exception as e:
        if "does not exist" in str(e):
            # Table not present: no window state available
            return None
        raise

    if row is None:
        return None

    # Parse and cache
    import json
    ws = json.loads(row["state"])

    # P1: Store in cache (only non-None values)
    if ws is not None:
        _window_cache_put(session_key, ws)

    return ws


async def _window_cleanup_ttl(max_age_hours: int = 24) -> int:
    """Delete window rows older than max_age_hours. Returns count deleted."""
    # TODO(I4): replace with AppContext
    from proxy.app import pool
    if not pool:
        return 0
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM \"window\" WHERE updated_at < now() - interval '$1 hours'",
                str(max_age_hours),
            )
        # asyncpg returns a status string like "DELETE 5"
        parts = result.strip().split()
        return int(parts[-1]) if len(parts) >= 2 else 0
    except Exception as e:
        if "does not exist" in str(e):
            return 0
        raise
