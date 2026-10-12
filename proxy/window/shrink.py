"""Emergency shrink: reduce context when it exceeds capacity."""
import logging

from proxy.token.counter import count_message_tokens, count_messages_tokens
from proxy.window.state import _msg_anchor
from proxy.window.elide import _elided_tool_content
from proxy.window.invariants import protected_tool_groups
from proxy.window.cut import _trim_target

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


class ContextCapacityError(Exception):
    """Raised when protected material alone exceeds the ceiling even after
    truncating the live prompt. This indicates a configuration problem
    (e.g., system prompt too large for the configured context window)."""
    def __init__(self, message: str, before: int, ceiling: int):
        super().__init__(message)
        self.before = before
        self.ceiling = ceiling


def _protected_indices(work: list) -> set:
    """Indices that _emergency_shrink must never drop: the seed (first 3), the
    pinned newest-user copy (flagged), and
    the canonical protected tool groups (newest 4 tool results + their assistant
    tool_calls declarations). Uses protected_tool_groups as the single source
    of truth for recent tool body protection."""
    protected = set(range(min(3, len(work))))
    # Canonical: newest 4 tool groups (replaces old "newest 6" ad-hoc logic)
    protected |= protected_tool_groups(work)
    # Live prompt (last user message) must NEVER be elided
    for k in range(len(work) - 1, -1, -1):
        if work[k].get("role") == "user":
            protected.add(k)
            break
    return protected


def _truncate_message_content(msg: dict, max_tokens: int) -> dict:
    """Truncate a message's content to fit within max_tokens.
    Returns a new dict with truncated content. Only works on string content."""
    content = msg.get("content")
    if not isinstance(content, str):
        return msg
    # Rough estimate: 1 token ~= 4 chars for English text
    max_chars = max_tokens * 4
    if len(content) <= max_chars:
        return msg
    truncated = content[:max_chars] + "\n[... truncated by ctxgate ...]"
    new_msg = dict(msg)
    new_msg["content"] = truncated
    return new_msg


def _emergency_shrink(kept: list, ceiling: int) -> list:
    """Hard invariant: shrink a built window to <= ceiling tokens without breaking
    the tool-call graph. Uses O(n) incremental token tracking (no re-counting in loops).
    
    Strategy (in order of destructiveness):
    1. Elide large tool bodies (oldest first, skip protected)
    2. Drop oldest whole turn groups (skip protected)
    3. Truncate the live prompt (newest user message) to fit remaining budget
    4. If STILL over ceiling -> raise ContextCapacityError (configuration issue)
    """
    import proxy.app as _app
    metrics = _app.metrics

    before = count_messages_tokens(kept)
    if before <= ceiling:
        return kept
    metrics["emergency_shrink_total"] += 1
    work = [dict(m) for m in kept]
    protected = _protected_indices(work)
    # Pre-compute per-message token estimates (O(n) once)
    msg_toks = [count_message_tokens(m) for m in work]
    running_total = sum(msg_toks)
    target = _trim_target()
    
    # (1) elide large tool bodies, oldest-first, SKIP protected
    for i in range(len(work)):
        if running_total <= target:
            break
        if i in protected:
            continue
        m = work[i]
        if m.get("role") != "tool":
            continue
        c = m.get("content")
        if not isinstance(c, str) or len(c) <= 2500:
            continue
        event_hash = _msg_anchor(m)[:16]
        new_content = _elided_tool_content(c, session_key='', event_hash=event_hash)
        old_tok = msg_toks[i]
        work[i]["content"] = new_content
        new_tok = count_message_tokens(work[i])
        running_total += new_tok - old_tok
        msg_toks[i] = new_tok
    
    # (2) drop oldest whole groups while still over target
    dropped_groups = 0
    while running_total > target:
        start = None
        for i in range(len(work)):
            if i in protected:
                continue
            if work[i].get("role") in ("assistant", "user"):
                start = i
                break
        if start is None:
            break  # No non-protected groups left to drop; fall through to step 3
        end = start
        if work[start].get("role") == "assistant" and work[start].get("tool_calls"):
            j = start + 1
            while j < len(work) and work[j].get("role") == "tool":
                end = j
                j += 1
        while end > start and end in protected:
            end -= 1
        for i in range(start, end + 1):
            running_total -= msg_toks[i]
        del work[start:end + 1]
        msg_toks = [count_message_tokens(m) for m in work]
        dropped_groups += 1
    metrics["emergency_shrink_groups_dropped"] += dropped_groups
    
    # (3) If still over ceiling, truncate the live prompt (newest user message)
    if running_total > ceiling:
        # Find the newest user message in work
        lu_idx = None
        for k in range(len(work) - 1, -1, -1):
            if work[k].get("role") == "user":
                lu_idx = k
                break
        if lu_idx is not None:
            # Calculate how many tokens we need to remove
            over_by = running_total - ceiling
            # Budget for the live prompt: its current tokens minus over_by,
            # but never less than a minimum (e.g., 200 tokens for the instruction)
            current_lu_tok = msg_toks[lu_idx]
            min_lu_tok = 200  # minimum viable instruction size
            target_lu_tok = max(min_lu_tok, current_lu_tok - over_by)
            if target_lu_tok < current_lu_tok:
                old_msg = work[lu_idx]
                work[lu_idx] = _truncate_message_content(old_msg, target_lu_tok)
                new_lu_tok = count_message_tokens(work[lu_idx])
                running_total += new_lu_tok - current_lu_tok
                msg_toks[lu_idx] = new_lu_tok
                metrics["emergency_shrink_live_truncated"] = metrics.get("emergency_shrink_live_truncated", 0) + 1
                log.warning("EMERGENCY SHRINK: truncated live prompt from %d to %d tokens (over_by=%d)",
                           current_lu_tok, new_lu_tok, over_by)
    
    # (4) Final check: if STILL over ceiling, it's a configuration problem
    if running_total > ceiling:
        log.error("EMERGENCY SHRINK: protected material exceeds ceiling even after live-prompt truncation (before=%d now=%d ceiling=%d)", before, running_total, ceiling)
        raise ContextCapacityError(
            f"Protected material (seed + tool groups + minimum live prompt) exceeds the safe budget "
            f"({running_total} tokens > ceiling {ceiling}). "
            f"Reduce the system prompt size, reduce tool definitions, or increase CTXGATE_MAX_CONTEXT.",
            before, ceiling)
    
    log.warning("EMERGENCY SHRINK: before=%d after=%d target=%d groups_dropped=%d", before, running_total, target, dropped_groups)
    return work
