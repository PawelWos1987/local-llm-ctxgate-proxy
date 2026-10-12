"""Goose session resolution from SQLite DB."""
import hashlib
import logging
import time
import uuid
from typing import Optional

import aiosqlite

log = logging.getLogger("ctxgate-proxy")


async def _get_goose_session_id() -> str:
    """Read the most recent active session ID from Goose sessions SQLite DB.
    Only active when CTXGATE_GOOSE_INTEGRATION=1. Returns empty string when disabled."""
    from proxy.app import CTXGATE_GOOSE_INTEGRATION, GOOSE_SESSIONS_DB, sqlite_conn, sqlite_lock
    import proxy.app as _app

    if not CTXGATE_GOOSE_INTEGRATION:
        return ""
    async with sqlite_lock:
        try:
            if _app.sqlite_conn is None:
                _app.sqlite_conn = await aiosqlite.connect(GOOSE_SESSIONS_DB)
            cursor = await _app.sqlite_conn.execute(
                "SELECT id FROM sessions WHERE archived_at IS NULL ORDER BY updated_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                return row[0]
        except Exception as e:
            log.debug("Goose session lookup failed: %s", e)
            try:
                if _app.sqlite_conn:
                    await _app.sqlite_conn.close()
                _app.sqlite_conn = None
            except Exception:
                pass
    return ""


async def _get_goose_session_info(session_id: str) -> Optional[dict]:
    """Fetch full session metadata from Goose SQLite DB by session ID."""
    from proxy.app import CTXGATE_GOOSE_INTEGRATION, GOOSE_SESSIONS_DB, sqlite_lock
    import proxy.app as _app

    if not CTXGATE_GOOSE_INTEGRATION:
        return None
    async with sqlite_lock:
        try:
            if _app.sqlite_conn is None:
                _app.sqlite_conn = await aiosqlite.connect(GOOSE_SESSIONS_DB)
            cursor = await _app.sqlite_conn.execute(
                "SELECT id, name, session_type, working_dir, provider_name FROM sessions WHERE id = ?",
                (session_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                return {
                    "id": row[0],
                    "name": row[1] or "",
                    "session_type": row[2] or "",
                    "working_dir": row[3] or "",
                    "provider_name": row[4] or "",
                }
        except Exception as e:
            log.debug("Goose session info lookup failed for %s: %s", session_id, e)
            try:
                if _app.sqlite_conn:
                    await _app.sqlite_conn.close()
                _app.sqlite_conn = None
            except Exception:
                pass
    return None


async def _resolve_goose_session(messages: list) -> Optional[dict]:
    """Try to resolve a Goose session from message content fingerprints."""
    from proxy.app import (
        CTXGATE_GOOSE_INTEGRATION, GOOSE_SESSION_UUID_NAMESPACE,
        CTXGATE_GOOSE_SESSION_LOOKUP_WINDOW_SEC, sqlite_lock,
    )
    import proxy.app as _app

    try:
        fp_msg = None
        for m in messages:
            if m.get('role') == 'user':
                c = m.get('content', '')
                if isinstance(c, list):
                    fp_msg = ' '.join(p.get('text', '') for p in c if isinstance(p, dict))
                else:
                    fp_msg = c
                break
        if not fp_msg:
            return None
        if _app.sqlite_conn is None:
            return None
        async with sqlite_lock:
            cursor = await _app.sqlite_conn.execute(
                "SELECT session_id, name, session_type, working_dir, provider_name FROM goose_sessions "
                "WHERE content_fp = ? AND ts > ? LIMIT 1",
                (hashlib.sha256(fp_msg.encode()).hexdigest(),
                 time.time() - CTXGATE_GOOSE_SESSION_LOOKUP_WINDOW_SEC)
            )
            row = await cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "uuid": str(uuid.uuid5(GOOSE_SESSION_UUID_NAMESPACE, row[0])),
                "name": row[1] or row[0],
                "session_type": row[2] or "",
                "working_dir": row[3] or "",
                "provider_name": row[4] or "",
            }
        return None
    except Exception:
        return None
