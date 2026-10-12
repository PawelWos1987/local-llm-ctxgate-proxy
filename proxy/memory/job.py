"""Memory job enqueueing and background task management."""
import asyncio
import os
import hashlib
import re

_ensure_task_done: set = set()
WORKER_BACKPRESSURE = int(os.environ.get("CTXGATE_WORKER_BACKPRESSURE", "50"))




async def _bg_ensure_task_and_enqueue(meta, tenant_id,
                                      session_id, user_content):
    """Chained background init: INSERT task row (once per
    session_key), then enqueue memory job. Never raises to
    the caller. Skips enqueue if INSERT fails."""
    from proxy.app import log  # TODO(I4): replace with AppContext
    from proxy.db.tasks import _ensure_task_row
    session_key = f"goose:{meta['id']}"
    if session_key not in _ensure_task_done:
        try:
            await _ensure_task_row(meta, tenant_id)
            _ensure_task_done.add(session_key)
        except Exception as e:
            log.warning(
                "task row init failed for %s (will retry): %s",
                session_key, e)
            return
    try:
        await _enqueue_memory_job(
            session_id, user_content,
            tenant_id=tenant_id, task_uuid=meta["uuid"])
    except Exception as e:
        log.warning(
            "_enqueue_memory_job failed for %s: %s",
            session_key, e)







async def _enqueue_memory_job(session_id, user_content, tenant_id: str = "", task_uuid=None):
    from proxy.app import log, metrics, pool  # TODO(I4): replace with AppContext
    from proxy.db.events import _insert_event_row
    from proxy.db.tasks import _resolve_task
    if os.environ.get("CTXGATE_MEMORY_WORKER", "1") == "0":
        return
    # --- Backpressure: LOG/report lag but NEVER drop an eligible durable event ---
    try:
        from proxy.app import _worker_pending_count
        pending = await _worker_pending_count()
        if pending and pending > WORKER_BACKPRESSURE:
            metrics["memory_worker_lag"] += 1
            metrics["memory_jobs_dropped"] += 1  # observability only; job is still enqueued
            log.warning("Backpressure: %d pending memory jobs > %d - logging lag (job NOT dropped)", pending, WORKER_BACKPRESSURE)
    except Exception:
        pass
    # --- Boilerplate filter: strip <turn-context> blocks ---
    import re
    cleaned = re.sub(r'<turn-context>.*?</turn-context>', '', user_content, flags=re.DOTALL).strip()
    if not cleaned or len(cleaned) < 30:
        return
    try:
        if task_uuid is None:
            task_uuid = await _resolve_task(session_id, create=True, tenant_id=tenant_id)
        if task_uuid is None:
            return
        # --- F13: compute the canonical envelope FIRST, then fingerprint it ---
        if len(cleaned) > 5000:
            head = cleaned[:3000]
            tail = cleaned[-1500:]
            omitted = len(cleaned) - 4500
            stored = head + "\n[..." + str(omitted) + " chars omitted...\n" + tail
        else:
            stored = cleaned
        new_fp = hashlib.sha256(stored.encode()).hexdigest()[:16]
        # --- Dedup: skip if last event has same content fingerprint ---
        last_row = await pool.fetchrow(
            'SELECT content FROM proxy.events WHERE task_id=$1 ORDER BY seq DESC, id DESC LIMIT 1',
            task_uuid)
        if last_row:
            # F13: fingerprint the stored content the same way (envelope of the stored string)
            last_content = last_row['content']
            if len(last_content) > 5000:
                l_head = last_content[:3000]
                l_tail = last_content[-1500:]
                l_omitted = len(last_content) - 4500
                last_envelope = l_head + "\n[..." + str(l_omitted) + " chars omitted...\n" + l_tail
            else:
                last_envelope = last_content
            last_fp = hashlib.sha256(last_envelope.encode()).hexdigest()[:16]
            if last_fp == new_fp:
                log.debug('Dedup: skipping duplicate enqueue for session %s', session_id)
                return
        ev_id, ns = await _insert_event_row(task_uuid, 'user', stored)
        await pool.execute(
            'INSERT INTO proxy.memory_jobs (task_id, event_id, status, tenant_id) VALUES ($1,$2,$3,$4)',
            task_uuid, ev_id, 'pending', tenant_id)
        metrics["memory_jobs_created"] += 1
        log.info('Enqueued memory job for session %s (seq %d)', session_id, ns)
    except Exception as e:
        metrics["memory_store_failure"] += 1
        log.warning('Memory job enqueue failed %s: %s', session_id, e)


