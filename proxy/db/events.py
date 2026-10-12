"""Database event insertion."""
import logging

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


async def _insert_event_row(task_uuid, role, content, meta=None):
    """Insert one proxy.events row with a race-safe per-task seq. Returns (event_id, seq)."""
    # TODO(I4): replace with AppContext
    from proxy.app import pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                'SELECT 1 FROM proxy.tasks WHERE id=$1 FOR UPDATE',
                task_uuid,
            )
            ns = await conn.fetchval(
                'SELECT COALESCE(MAX(seq), -1) + 1 FROM proxy.events WHERE task_id=$1',
                task_uuid,
            )
            if meta is None:
                eid = await conn.fetchval(
                    'INSERT INTO proxy.events (task_id, seq, role, content) '
                    'VALUES ($1,$2,$3,$4) RETURNING id',
                    task_uuid, ns, role, content,
                )
            else:
                eid = await conn.fetchval(
                    'INSERT INTO proxy.events (task_id, seq, role, content, meta) '
                    'VALUES ($1,$2,$3,$4,$5) RETURNING id',
                    task_uuid, ns, role, content, meta,
                )
            return eid, ns
