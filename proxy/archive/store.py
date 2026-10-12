"""Archived content storage and retrieval."""
import hashlib
from proxy.ledger.format import TOOL_BODY_CHUNK_CHARS



async def _retrieve_archived_content(
    tenant_id: str,
    session_key: str,
    event_hash: str,
    start: int = 0,
    end: int = None,
) -> str:
    """Fetch archived tool body content by tenant + session + event_hash + range."""
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    if not pool:
        return ""
    try:
        if end is None:
            rows = await pool.fetch(
                "SELECT content FROM proxy.tool_body_chunks "
                "WHERE tenant_id=$1 AND session_key=$2 AND event_hash=$3 AND chunk_idx >= $4 "
                "ORDER BY chunk_idx",
                tenant_id, session_key, event_hash, start,
            )
        else:
            rows = await pool.fetch(
                "SELECT content FROM proxy.tool_body_chunks "
                "WHERE tenant_id=$1 AND session_key=$2 AND event_hash=$3 "
                "AND chunk_idx >= $4 AND chunk_idx < $5 "
                "ORDER BY chunk_idx",
                tenant_id, session_key, event_hash, start, end,
            )
        return "".join(r["content"] for r in rows)
    except Exception as e:
        log.warning("_retrieve_archived_content failed: %s", e)
        return ""





async def _store_tool_body(
    tenant_id: str,
    session_key: str,
    event_hash: str,
    content: str,
    chunk_size: int = None,
) -> int:
    """Store a large tool body in chunks. Idempotent (ON CONFLICT DO NOTHING)."""
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    from proxy.archive.chunk import _chunk_tool_body
    if not pool:
        return 0
    if chunk_size is None:
        chunk_size = TOOL_BODY_CHUNK_CHARS
    chunks = _chunk_tool_body(content, chunk_size)
    if not chunks:
        return 0
    total = len(chunks)
    stored = 0
    for i, chunk in enumerate(chunks):
        chunk_hash = hashlib.sha256(chunk.encode()).hexdigest()
        try:
            await pool.execute(
                "INSERT INTO proxy.tool_body_chunks "
                "(tenant_id, session_key, event_hash, chunk_idx, content, content_hash, total_chunks) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (session_key, event_hash, chunk_idx) DO NOTHING",
                tenant_id, session_key, event_hash, i, chunk, chunk_hash, total,
            )
            stored += 1
        except Exception as e:
            log.debug("Tool body chunk store failed: %s", e)
    return stored



