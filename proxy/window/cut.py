"""Window cut logic: turn grouping, safe cutting, recutting."""
import logging
import re

from proxy.config.constants import (
    CTXGATE_WINDOW_TRIGGER_FRAC, CTXGATE_WINDOW_TARGET_FRAC,
    MAX_INPUT, MAX_CONTEXT, SAFETY_MARGIN, MAX_OUTPUT, TRIM_TARGET_FLOOR, TRIM_TARGET_TOKENS, CUT_QUANTUM,
    CTXGATE_PREFIX_DIAG_ON, CTXGATE_INJECT_EPOCH_FREEZE_ON,
    CTXGATE_INJECT_MAX_TOKENS_CFG, CTXGATE_INJECT_MAX_TOKENS_RECAP_CFG,
)
from proxy.window.state import _norm_content, _msg_anchor, _seed_sig, _newest_user_idx
from proxy.token.exact import exact_count_messages
from proxy.config.constants import VLLM_MODEL

log = logging.getLogger("ctxgate-proxy")

# Module-level metrics counter (TODO(I4): replace with AppContext)
metrics: dict[str, int] = {}

# TODO(I4): replace with AppContext


def _eviction_trigger() -> int:
    """High-watermark trigger: eviction triggers when kept tokens exceed this."""
    return int(MAX_INPUT * CTXGATE_WINDOW_TRIGGER_FRAC)

def _trim_target() -> int:
    """Low-watermark target after eviction."""
    floor_eff = min(TRIM_TARGET_FLOOR, MAX_INPUT // 2)
    if TRIM_TARGET_TOKENS > 0:
        t = TRIM_TARGET_TOKENS
    else:
        t = int(MAX_INPUT * CTXGATE_WINDOW_TARGET_FRAC)
    return max(floor_eff, min(t, MAX_INPUT))
def _turn_groups(messages: list) -> list:
    """Parse messages into turn groups. A turn group is a maximal sequence
    starting with a user message, followed by zero or more (assistant + tool
    results) pairs. The group boundary is always at a user message.
    
    Returns a list of lists, where each inner list is a turn group.
    Messages before the first user message (e.g., system) form their own group.
    """
    groups = []
    current = []
    for m in messages:
        if m.get("role") == "user" and current:
            groups.append(current)
            current = []
        current.append(m)
    if current:
        groups.append(current)
    return groups

def _safe_cut(groups: list, budget_tokens: int) -> int:
    """Turn-group-aware budget cut with quantized motion.
    
    Cut motion is quantized to CUT_QUANTUM-token steps: the effective
    budget is rounded DOWN to the nearest quantum boundary before
    comparison. Never cuts in the middle of a turn group."""
    if not groups:
        return 0
    quantized_budget = (budget_tokens // CUT_QUANTUM) * CUT_QUANTUM
    if quantized_budget < 512:
        quantized_budget = budget_tokens
    group_tokens = []
    for g in groups:
        gt = sum(exact_count_messages([m], VLLM_MODEL) for m in g)
        group_tokens.append(gt)
    cum = 0
    cut_group_idx = len(groups)
    for i in range(len(groups) - 1, -1, -1):
        cum += group_tokens[i]
        if cum <= quantized_budget:
            cut_group_idx = i
        else:
            if cum <= quantized_budget + CUT_QUANTUM:
                cut_group_idx = i
            break
    return cut_group_idx
def _recut_to(messages: list, max_tokens: int, total_tokens: int | None = None) -> int:
    """Turn-group-aware budget cut targeting max_tokens.

    The immutable prefix is the system message (index 0) + the first user message.
    All other messages are ordinary history subject to budget cutting.
    Cuts are quantized to turn group boundaries - never in the middle of a
    turn group. If the budget boundary falls inside a group, extend to the
    end of that group (or start of next group, whichever keeps fewer tokens).
    """
    # TODO(I4): replace with AppContext
    from proxy.app import _note_tok, metrics
    # Immutable prefix: system message (index 0) + first user message
    seed = []
    rest = list(messages)
    if messages and messages[0].get("role") == "system":
        seed.append(messages[0])
        rest = list(messages[1:])
    # Add first user message to the immutable seed
    for i, m in enumerate(rest):
        if m.get("role") == "user":
            seed.append(m)
            rest = rest[:i] + rest[i+1:]
            break
    
    stub_tok = _note_tok()
    seed_tok = exact_count_messages(seed, VLLM_MODEL) if seed else 0  # [RULE 1: pre-vLLM]
    total_tok = total_tokens if total_tokens is not None else exact_count_messages(messages, VLLM_MODEL)  # [RULE 1: pre-vLLM]
    lu = _newest_user_idx(rest)
    budget = max(500, max_tokens - seed_tok - stub_tok - 200)
    
    # Turn-group-aware cutting
    groups = _turn_groups(rest)
    if not groups:
        return 0
    
    cut_group_idx = _safe_cut(groups, budget)
    
    # Convert group index to message index within rest
    cut = 0
    for i in range(cut_group_idx):
        cut += len(groups[i])
    
    # Post-check: guard against window collapse
    kept = _kept_messages(seed, rest, cut)
    # [allowed: post-cut verification]
    post_cut_tokens = exact_count_messages(kept, VLLM_MODEL)
    if post_cut_tokens < 0.5 * max_tokens and total_tok > max_tokens and len(rest) > 0:
        log.warning("WINDOW COLLAPSE guard: post_cut_tokens=%d target=%d total_tok=%d cut=%d rest=%d - middle-trim fallback",
                      post_cut_tokens, max_tokens, total_tok, cut, len(rest))
        metrics["window_collapse_guard"] += 1

        # Middle-trim fallback: preserve maximum context while staying under budget.
        # Strategy: progressively deepen the prefix cut only as much as needed.
        # Never cuts the seed (already excluded). Always preserves the newest user msg.
        lu = _newest_user_idx(rest)

        # Attempt 1: keep everything (cut=0) — maybe the initial cut was overly aggressive
        test_kept = _kept_messages(seed, rest, 0)
        test_tok = exact_count_messages(test_kept, VLLM_MODEL)
        if test_tok <= max_tokens:
            cut = 0
        elif lu is not None:
            # Attempt 2: cut to the newest user message (preserves live prompt + tail)
            test_kept = _kept_messages(seed, rest, lu)
            test_tok = exact_count_messages(test_kept, VLLM_MODEL)
            if test_tok <= max_tokens:
                cut = lu
            else:
                # Attempt 3: incrementally deepen cut from lu until under budget
                cut = lu
                while cut < len(rest) - 1:
                    test_kept = _kept_messages(seed, rest, cut)
                    test_tok = exact_count_messages(test_kept, VLLM_MODEL)
                    if test_tok <= max_tokens:
                        break
                    cut += 1
        else:
            # No newest user found; incremental cut from start
            cut = 0
            while cut < len(rest) - 1:
                test_kept = _kept_messages(seed, rest, cut)
                test_tok = exact_count_messages(test_kept, VLLM_MODEL)
                if test_tok <= max_tokens:
                    break
                cut += 1
    
    # Over-shoot guard: estimate undercounted -> kept is still over budget
    kept = _kept_messages(seed, rest, cut)
    # [allowed: post-cut verification]
    post_cut_tokens = exact_count_messages(kept, VLLM_MODEL)
    if post_cut_tokens > max_tokens and cut_group_idx < len(groups):
        log.warning("RECURT OVER-SHOOT: post_cut_tokens=%d target=%d cut=%d groups=%d - cutting deeper",
                    post_cut_tokens, max_tokens, cut, len(groups))
        metrics["recut_overshoot_guard"] += 1
        while cut_group_idx < len(groups):
            cut_group_idx += 1
            cut = sum(len(groups[i]) for i in range(cut_group_idx))
            kept = _kept_messages(seed, rest, cut)
            # [allowed: post-cut verification]
            post_cut_tokens = exact_count_messages(kept, VLLM_MODEL)
            if post_cut_tokens <= max_tokens:
                break

    # Guard: never cut past the newest user message (live prompt / protected region)
    if lu is not None and cut > lu:
        log.warning("RECUT GUARD: clamping cut from %d to %d (newest user msg at rest[%d] protected)", cut, lu, lu)
        metrics["recut_user_pinned"] = metrics.get("recut_user_pinned", 0) + 1
        cut = lu

    # Apply the cut in place
    if cut > 0:
        if seed:
            messages[:] = seed + rest[cut:]
        else:
            messages[:] = rest[cut:]
    return cut


def _repair_tool_dependencies(messages: list, to_remove_set: set) -> int:
    """Ensure tool-call dependency consistency in the removal set.
    
    If an assistant message with tool_calls is in to_remove_set, all tool
    messages referencing those tool_call_ids must also be in to_remove_set.
    Conversely, if a tool message is NOT in to_remove_set but its parent
    assistant IS, the tool message must be added.
    
    Modifies to_remove_set in place. Returns the number of messages added.
    """
    # Build map: tool_call_id -> index of assistant message that declared it
    tcid_to_assistant = {}
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id")
                if tcid:
                    tcid_to_assistant[tcid] = i

    added = 0

    # Pass 1: For each tool message NOT in to_remove_set, check if its
    # parent assistant IS in to_remove_set. If so, the tool must go too.
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and i not in to_remove_set:
            tcid = m.get("tool_call_id")
            if tcid and tcid in tcid_to_assistant:
                parent_idx = tcid_to_assistant[tcid]
                if parent_idx in to_remove_set:
                    to_remove_set.add(i)
                    added += 1

    # Pass 2: For each assistant IN to_remove_set that has tool_calls,
    # ensure all corresponding tool messages are also removed.
    for i, m in enumerate(messages):
        if i in to_remove_set and m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id")
                if tcid:
                    for j, tm in enumerate(messages):
                        if tm.get("role") == "tool" and tm.get("tool_call_id") == tcid and j not in to_remove_set:
                            to_remove_set.add(j)
                            added += 1

    if added:
        log.info("TOOL-DEP REPAIR: added %d tool messages to removal set to prevent orphans", added)

    return added


def _hard_cut_to(messages: list, actual_tokens: int, target_tokens: int) -> int:
    """Hard cut: remove exactly (actual_tokens - target_tokens) tokens from oldest messages.

    Uses vLLM's exact count as ground truth. No estimation involved in the
    'how much to remove' decision. Only uses estimation for turn-boundary
    alignment (where to cut).

    Preserves: system message (index 0), newest user message (live prompt).
    Removes from oldest non-protected messages first.

    Tool-call dependency: if an assistant message with tool_calls is removed,
    all associated tool messages are also removed to prevent orphans.

    Returns the number of tokens removed (0 if nothing to remove).
    """
    if actual_tokens <= target_tokens:
        return 0

    tokens_to_remove = actual_tokens - target_tokens

    # Identify protected messages: system (0) and newest user
    if not messages:
        return 0

    # Find newest user index
    newest_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            newest_user_idx = i
            break

    # Build list of removable indices (skip system at 0, skip newest user)
    removable = []
    for i in range(len(messages)):
        if i == 0 and messages[i].get("role") == "system":
            continue
        if i == newest_user_idx:
            continue
        removable.append(i)

    if not removable:
        return 0

    # Walk from oldest, accumulate tokens until we've removed enough
    removed = 0
    to_remove_set = set()
    for i in removable:
        if removed >= tokens_to_remove:
            break
        mt = exact_count_messages([messages[i]], VLLM_MODEL)
        to_remove_set.add(i)
        removed += mt

    # Tool-call dependency repair: ensure no orphan tool messages survive
    _repair_tool_dependencies(messages, to_remove_set)

    # Recalculate removed tokens after dependency repair
    removed = sum(exact_count_messages([messages[i]], VLLM_MODEL) for i in to_remove_set)

    # Build new list excluding removed messages
    kept = [m for i, m in enumerate(messages) if i not in to_remove_set]
    messages[:] = kept

    log.info("HARD CUT: removed %d tokens (%d msgs) to go from %d to ~%d",
             removed, len(to_remove_set), actual_tokens, actual_tokens - removed)

    return removed
def _recut(messages: list) -> int:
    return _recut_to(messages, _trim_target())



def _kept_messages(seed: list, rest: list, cut: int) -> list:
    """Rebuild the kept window: seed + rest[cut:].
    If the newest user message (live prompt) sits before the cut, the cut is
    extended to include it so the live prompt is always present at its natural
    position. No duplicate/pinned copy is created.
    
    If the live prompt is so large that seed + live_prompt alone would exceed
    the budget, the live prompt content is truncated to fit (keeping head and
    tail)."""
    lu = _newest_user_idx(rest)
    extended = False
    if lu is not None and lu < cut:
        cut = lu
        extended = True
    # Align cut boundary: skip leading tool messages whose parent assistant
    # was dropped (would create orphan_tool invariant violations).
    while cut < len(rest) and rest[cut].get("role") == "tool":
        cut += 1
    
    result = list(seed)
    result.extend(rest[cut:])
    
    # Safety: if the live prompt dominates the window, truncate it
    # (extended=True means we pulled the live prompt back into the window)
    if extended and lu is not None:
        live_idx_in_result = len(seed)  # rest[cut:] starts at rest[lu] since cut==lu
        if live_idx_in_result < len(result):
            live_msg = result[live_idx_in_result]
            live_tok = exact_count_messages([live_msg], VLLM_MODEL)  # [RULE 3: post-cut]
            seed_tok = exact_count_messages(seed, VLLM_MODEL)  # [RULE 3: post-cut] if seed else 0
            budget = MAX_INPUT
            max_live = budget - seed_tok - 500  # 500 for stub/overhead
            if max_live < 1000:
                max_live = 1000  # absolute minimum
            if live_tok > max_live:
                _truncate_message_content(live_msg, max_live)
                log.warning("LIVE PROMPT TRUNCATED: was=%d tokens, capped to ~%d (seed=%d budget=%d)",
                           live_tok, max_live, seed_tok, budget)
                metrics["live_prompt_truncated"] = metrics.get("live_prompt_truncated", 0) + 1
    
    return result

def _truncate_message_content(msg: dict, max_tokens: int) -> None:
    """Truncate a message's content in-place to fit within max_tokens.
    
    Keeps the first 60% and last 40% of the content with a truncation marker
    in the middle. This preserves both the beginning (context/instructions)
    and the end (most recent/relevant content)."""
    content = msg.get("content")
    if not isinstance(content, str) or not content:
        return
    
    # Rough: 1 token ~ 4 chars for English
    max_chars = max_tokens * 4
    if len(content) <= max_chars:
        return
    
    # Split: 60% head, 40% tail
    head_len = int(max_chars * 0.6)
    tail_len = int(max_chars * 0.4)
    marker = f"\n[... {len(content) - head_len - tail_len} chars truncated ...]\n"
    msg["content"] = content[:head_len] + marker + content[-tail_len:]


def _is_context_length_error(status_code: int, error_body: str) -> bool:
    """Detect if an error is specifically a context-length overflow.

    Only 400 responses with context-length-specific messages qualify.
    401, 403, 429, 500, 503, etc. do NOT trigger a recut.
    """
    if status_code != 400:
        return False
    _et = error_body.lower()
    indicators = (
        "context length",
        "maximum context",
        "too many tokens",
        "context_length_exceeded",
        "maximum context length",
        "exceeds the model",
        "input is too long",
        "prompt is too long",
    )
    return any(ind in _et for ind in indicators)


def _parse_vllm_actual_tokens(error_body: str) -> dict:
    """Parse token counts from vLLM's context-length error.

    vLLM format: "This model's maximum context length is {MAX_CONTEXT} tokens.
    However, you requested {MAX_OUTPUT} output tokens and your prompt contains
    at least {N} input tokens, for a total of at least {TOTAL}"

    Returns dict with keys: max_context, requested_output, actual_input, total.
    Values are ints or None if not found.
    """
    result: dict = {"max_context": None, "requested_output": None, "actual_input": None, "total": None}

    # Primary: match the full sentence pattern
    m = re.search(
        r"maximum context length is (\d+) tokens.*?you requested (\d+) output tokens.*?"
        r"prompt contains at least (\d+) input tokens.*?total of at least (\d+)",
        error_body,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        result["max_context"] = int(m.group(1))
        result["requested_output"] = int(m.group(2))
        result["actual_input"] = int(m.group(3))
        result["total"] = int(m.group(4))
        return result

    # Partial fallbacks (individual patterns)
    m_ctx = re.search(r"maximum context length is (\d+) tokens", error_body, re.IGNORECASE)
    if m_ctx:
        result["max_context"] = int(m_ctx.group(1))

    m_out = re.search(r"you requested (\d+) output tokens", error_body, re.IGNORECASE)
    if m_out:
        result["requested_output"] = int(m_out.group(1))

    m_in = re.search(r"at least (\d+) input tokens", error_body, re.IGNORECASE)
    if m_in:
        result["actual_input"] = int(m_in.group(1))

    m_tot = re.search(r"total of at least (\d+)", error_body, re.IGNORECASE)
    if m_tot:
        result["total"] = int(m_tot.group(1))

    return result
