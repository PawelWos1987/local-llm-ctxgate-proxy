"""Tool body chunking for archival."""
import hashlib
from proxy.ledger.format import TOOL_BODY_CHUNK_CHARS



def _chunk_tool_body(content: str, chunk_size: int = None) -> list:
    """Split large tool output into bounded chunks for durable storage."""
    if chunk_size is None:
        chunk_size = TOOL_BODY_CHUNK_CHARS
    if len(content) <= chunk_size:
        return [content] if content else []
    chunks = []
    for i in range(0, len(content), chunk_size):
        chunks.append(content[i:i + chunk_size])
    return chunks





def _retrieve_chunk_range(chunks: list, start: int = 0, end: int = None) -> list:
    """Get a range of chunks (0-indexed, inclusive start, exclusive end)."""
    if end is None:
        end = len(chunks)
    start = max(0, start)
    end = min(len(chunks), end)
    if start >= end:
        return []
    return chunks[start:end]





def _deterministic_chunk_id(task_id: str, start_idx: int, end_idx: int, chunk_seq: int) -> str:
    """Generate a deterministic chunk ID from its components. Idempotent."""
    from proxy.app import _RESTART_RECOVERY_SQL  # TODO(I4): replace with AppContext
    raw = "%s|%d|%d|%d" % (task_id, start_idx, end_idx, chunk_seq)
    return hashlib.sha256(raw.encode()).hexdigest()

# Restart recovery SQL: find orphaned chunks with expired leases
_RESTART_RECOVERY_SQL = (
    "UPDATE proxy.summary_chunks "
    "SET state='queued', owner=NULL, lease_expires=NULL, updated_at=now() "
    "WHERE state IN ('claimed','processing') AND lease_expires < now()"
)



