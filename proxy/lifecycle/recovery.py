"""Restart recovery and task spawning helpers."""
import asyncio
import logging

log = logging.getLogger("ctxgate-proxy")

def _spawn(coro):
    """Create a task and keep a strong reference so GC cannot destroy it while pending."""
    from proxy.app import _background_tasks
    t = asyncio.ensure_future(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)

async def _restart_recovery() -> int:
    """On startup: reset orphaned chunks (expired leases) back to 'queued'."""
    from proxy.app import pool, _RESTART_RECOVERY_SQL
    if not pool:
        return 0
    try:
        result = await pool.execute(_RESTART_RECOVERY_SQL)
        parts = result.split()
        count = int(parts[-1]) if parts else 0
        if count > 0:
            log.info("Restart recovery: reset %d orphaned chunks to queued", count)
        return count
    except Exception as e:
        log.warning("Restart recovery failed: %s", e)
        return 0
