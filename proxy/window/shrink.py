"""Emergency shrink: reduce context when it exceeds capacity."""
import logging

from proxy.token.exact import exact_count_messages
from proxy.config.constants import VLLM_MODEL
from proxy.window.state import _msg_anchor
from proxy.window.elide import _elided_tool_content
from proxy.window.invariants import protected_tool_groups
from proxy.window.cut import _trim_target

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext

# Prefixes that identify the proxy-injected block within the last user message.
# When allow_injection_prune=True, _emergency_shrink can remove these.
_INJECTION_PREFIXES = ("Relevant knowledge:", "Task memory:", "Session digest:")


class ContextCapacityError(Exception):
    """Raised when protected material alone exceeds the ceiling even after
    truncating the live prompt. This indicates a configuration problem
    (e.g., system prompt too large for the configured context window)."""
    def __init__(self, message: str, before: int, ceiling: int):
        super().__init__(message)
        self.before = before
        self.ceiling = ceiling


def _protected_indices(work: list, aggressive: bool = False) -> set:
    """Indices that _emergency_shrink must never drop.

    Standard mode: seed (system + first user) + protected tool groups + live prompt.
    Aggressive mode: seed (system + first user) + live prompt ONLY (tool groups unprotected).
    """
    # Protect the immutable seed: system message + first user message
    protected = set()
    for i, m in enumerate(work):
        if m.get("role") == "system":
            protected.add(i)
        elif m.get("role") == "user" and i < 3:
            # First user message is part of the immutable seed
            protected.add(i)
            break
    if not aggressive:
        # Canonical: newest 4 tool groups
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
    if max_tokens is None:
        max_tokens = 0
    max_tokens = max(1, int(max_tokens))

    content = msg.get("content")
    if not isinstance(content, str):
        return msg
    # Using exact gigatoken-based token counting (replaces legacy char estimation)
    max_chars = max(1, int(max_tokens * 4))
    if len(content) <= max_chars:
        return msg
    truncated = content[:max_chars] + "\n[... truncated by ctxgate ...]"
    msg["content"] = truncated
    return msg


def _strip_injection_block(content: str) -> str:
    """Remove the proxy-injected block from a message's content string.

    The injection block consists of sections prefixed with:
    - "Relevant knowledge:"
    - "Task memory:"
    - "Session digest:"

    Returns the content with the injection block removed. If no injection
    block is found, returns the original content unchanged.
    """
    if not isinstance(content, str):
        return content

    # Find the earliest injection prefix
    earliest_pos = -1
    for prefix in _INJECTION_PREFIXES:
        pos = content.find(prefix)
        if pos != -1:
            if earliest_pos == -1 or pos < earliest_pos:
                earliest_pos = pos

    if earliest_pos == -1:
        return content

    # Remove everything from the earliest injection prefix to the end
    # (the injection block is always appended at the end of the user message)
    stripped = content[:earliest_pos].rstrip()
    return stripped if stripped else content


def _emergency_shrink(kept: list, ceiling: int, aggressive: bool = False,
                      allow_injection_prune: bool = False,
                      actual_total: int = 0) -> list:
    """Hard invariant: shrink a built window to <= ceiling tokens without breaking
    the tool-call graph. Uses O(n) incremental token tracking (no re-counting in loops).
    
    Strategy (in order of destructiveness):
    1. Elide large tool bodies (oldest first, skip protected)
    2. Drop oldest whole turn groups (skip protected)
    3. If allow_injection_prune=True: remove the injection block from the live prompt
    4. Truncate the live prompt (newest user message) to fit remaining budget
    5. If STILL over ceiling -> raise ContextCapacityError (configuration issue)
    
    When aggressive=True, the protection set is reduced to only the seed (first 3)
    and the live prompt (last user message). Tool groups are NOT protected, allowing
    much more aggressive reduction when the standard shrink cannot meet the ceiling.

    When allow_injection_prune=True (used by the reconciliation path), the function
    is additionally allowed to remove the proxy-injected block (knowledge + memory +
    digest) from the last user message before attempting live-prompt truncation.

    When actual_total > 0, it is used as the authoritative total token count for the
    initial "do we need to shrink?" check. This accounts for non-message overhead
    (tool definitions, template separators, injection) that exact_count_messages
    does not include. Without this, the function can exit early when message tokens
    are below the ceiling but the full payload (messages + overhead) exceeds it.
    """
    import proxy.app as _app
    metrics = _app.metrics

    # Use actual_total (real token count) as the authoritative source.
    # If not provided, proceed with shrink (conservative approach).
    # Use actual_total (full payload estimate) if provided, as it accounts for
    # non-message overhead (tools, template, injection) that message tokens miss.
    if actual_total > 0 and actual_total <= ceiling:
        return kept
    metrics["emergency_shrink_total"] += 1
    if aggressive:
        metrics["emergency_shrink_aggressive"] = metrics.get("emergency_shrink_aggressive", 0) + 1
    work = [dict(m) for m in kept]
    protected = _protected_indices(work, aggressive=aggressive)
    # Pre-compute per-message token counts (O(n) once)
    # Prefer _tokens (real HF tokenizer count from annotate_message_usage) over char-based estimate
    msg_toks = [m.get("_tokens") if m.get("_tokens") is not None else exact_count_messages([m], VLLM_MODEL) for m in work]
    running_total = sum(msg_toks)
    before = running_total
    # Target: max message tokens such that (messages + overhead) <= ceiling.
    # Overhead = actual_total - running_total (tools, system, injection).
    # Uses a DIFFERENCE (stable across calls), not a ratio (which ratchets).
    if actual_total > 0 and actual_total > running_total:
        overhead = actual_total - running_total
        # Guard: if overhead exceeds the ceiling itself, the actual_total is
        # stale (e.g., from a pre-shrink vLLM response). A real overhead
        # (tools + template) is at most a few thousand tokens.
        if overhead > ceiling:
            log.debug("EMERGENCY SHRINK: stale baseline detected (overhead=%d > ceiling=%d) - using trim_target", overhead, ceiling)
            target = min(_trim_target(), ceiling)
        else:
            target = ceiling - overhead
    else:
        target = min(_trim_target(), ceiling)
    target = max(target, 0)
    
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
        new_tok = exact_count_messages([work[i]], VLLM_MODEL)
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
        del msg_toks[start:end + 1]
        dropped_groups += 1
    metrics["emergency_shrink_groups_dropped"] += dropped_groups
    
    # (3) If allow_injection_prune and still over ceiling, remove the injection
    #     block from the live prompt before truncating it.
    if allow_injection_prune and running_total > ceiling:
        lu_idx = None
        for k in range(len(work) - 1, -1, -1):
            if work[k].get("role") == "user":
                lu_idx = k
                break
        if lu_idx is not None:
            content = work[lu_idx].get("content")
            if isinstance(content, str):
                stripped = _strip_injection_block(content)
                if stripped != content:
                    old_tok = msg_toks[lu_idx]
                    work[lu_idx]["content"] = stripped
                    new_tok = exact_count_messages([work[lu_idx]], VLLM_MODEL)
                    running_total += new_tok - old_tok
                    msg_toks[lu_idx] = new_tok
                    metrics["emergency_shrink_injection_pruned"] = metrics.get("emergency_shrink_injection_pruned", 0) + 1
                    log.info("EMERGENCY SHRINK: pruned injection block from live prompt (saved %d tokens)",
                             old_tok - new_tok)
    
    # (4) If still over ceiling, truncate the live prompt (newest user message)
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
                new_lu_tok = exact_count_messages([work[lu_idx]], VLLM_MODEL)
                running_total += new_lu_tok - current_lu_tok
                msg_toks[lu_idx] = new_lu_tok
                metrics["emergency_shrink_live_truncated"] = metrics.get("emergency_shrink_live_truncated", 0) + 1
                log.warning("EMERGENCY SHRINK: truncated live prompt from %d to %d tokens (over_by=%d)",
                           current_lu_tok, new_lu_tok, over_by)
    
    # (5) Final check: if STILL over ceiling, it's a configuration problem
    if running_total > ceiling:
        log.error("EMERGENCY SHRINK: protected material exceeds ceiling even after live-prompt truncation (before=%d now=%d ceiling=%d)", before, running_total, ceiling)
        raise ContextCapacityError(
            f"Protected material (seed + tool groups + minimum live prompt) exceeds the safe budget "
            f"({running_total} tokens > ceiling {ceiling}). "
            f"Reduce the system prompt size, reduce tool definitions, or increase CTXGATE_MAX_CONTEXT.",
            before, ceiling)
    
    log.warning("EMERGENCY SHRINK: before=%d after=%d target=%d groups_dropped=%d", before, running_total, target, dropped_groups)
    return work
