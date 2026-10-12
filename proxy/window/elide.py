"""Stable elision of tool content in the window."""
import logging
import hashlib

from proxy.window.state import _msg_anchor
from proxy.window.invariants import protected_tool_groups
log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


def _elided_tool_content(c: str, session_key: str = '', event_hash: str = '') -> str:
    """Deterministic elision: 1000-char head + marker + 1000-char tail.
    Includes SHA-256 content hash and retrieval handle. Idempotent."""
    if 'chars elided by ctxgate' in c:
        return c
    content_hash = hashlib.sha256(c.encode()).hexdigest()[:16]
    elided = len(c) - 2000
    if session_key and event_hash:
        handle = '/retrieve?session=%s&event=%s' % (session_key, event_hash)
    else:
        handle = '/retrieve?event=%s' % content_hash
    marker = '\n[...%d chars elided by ctxgate | hash=%s | retrieve: %s...]\n' % (elided, content_hash, handle)
    return c[:1000] + marker + c[-1000:]
def _stable_elide_inplace(msgs: list, start: int, end: int, session_key: str = '') -> None:
    """Elide in-place every tool message with string content > 2500 chars
    in msgs[start:end]. Produces head+tail with content hash and retrieval
    handle. IDEMPOTENT: same message elides to same bytes every call.
    Never touches non-tool messages."""
    if start < 0:
        start = 0
    end = min(end, len(msgs))
    if start >= end:
        return
    for i in range(start, end):
        m = msgs[i]
        if m.get('role') != 'tool':
            continue
        c = m.get('content')
        if isinstance(c, str) and len(c) > 2500:
            event_hash = _msg_anchor(m)[:16]
            m['content'] = _elided_tool_content(c, session_key=session_key, event_hash=event_hash)
def _stable_elide_idx(cut: int, rest: list) -> int:
    """Absolute index into raw 'rest' marking the elidable boundary:
    the index of the 4th-newest tool-result message (or 'cut', whichever is
    larger). Everything in [cut, idx) is elidable; [idx, end) keeps the
    newest 4 tool bodies intact (plus any interleaved assistant/user msgs).
    Uses protected_tool_groups as the canonical definition."""
    prot = protected_tool_groups(rest)
    tool_indices = sorted([i for i in prot if rest[i].get("role") == "tool"])
    if tool_indices:
        idx = tool_indices[0]  # oldest of the newest-4
    else:
        idx = len(rest)
    return max(cut, idx)

