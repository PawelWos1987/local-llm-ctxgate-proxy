"""Budget calculation: safe admitted budget, output budget, token estimation."""
import logging

from proxy.config.constants import MAX_INPUT, MAX_CONTEXT, SAFETY_MARGIN, MAX_OUTPUT, ESTIMATOR_MODE, CTXGATE_INPUT_ESTIMATE_HEADROOM
from proxy.token.counter import count_messages_tokens, count_tools_tokens
from proxy.token.calibrator import calibrator

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


def _safe_admitted_budget() -> int:
    """The safe admitted input budget: the effective ceiling for input tokens.

    The -1 accounts for the absolute floor in _output_budget
    (MAX_CONTEXT - effective_input - SAFETY_MARGIN - 1).
    Without it, a perfect shrink to this ceiling yields budget = MAX_OUTPUT - 1
    (off-by-one). The corrected formula guarantees budget >= MAX_OUTPUT when
    input is shrunk exactly to this ceiling.
    """
    return min(MAX_INPUT, int((MAX_CONTEXT - SAFETY_MARGIN - 1 - MAX_OUTPUT) / (1.0 + CTXGATE_INPUT_ESTIMATE_HEADROOM)))


def _estimate_delta(messages: list) -> int:
    """Estimate tokens for NEW content only (since last real baseline).

    The last_real baseline includes all messages from the previous request.
    The new content is everything added AFTER the previous request's last
    user message (i.e., the assistant's response + any new user messages).

    Strategy:
    - Find the second-to-last user message (the one that triggered the
      previous response). Everything after it is new content.
    - If there's only one user message, we can't determine what's new,
      so return 0 (the baseline already covers it).
    """
    if not messages:
        return 0

    # Find all user message indices
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]

    if len(user_indices) <= 1:
        # Only one (or zero) user message: can't determine new content.
        # The baseline already accounts for this message.
        return 0

    # New content starts after the second-to-last user message
    # (that was the last message in the previous request)
    start_idx = user_indices[-2] + 1
    new_msgs = messages[start_idx:]
    return count_messages_tokens(new_msgs)


def estimate_full_payload_tokens(messages: list, tools: list, note_tokens: int,
                                  session_key: str = "") -> int:
    """Canonical full-payload token estimator. SINGLE authority for admission checks.

    Counts ALL components of the outgoing request:
    - Static system prefix (within messages)
    - All retained messages (role + content + tool_calls)
    - Role/template separators (chat template overhead)
    - Injected checkpoint/digest (note_tokens)
    - Retrieved evidence (within messages)
    - Live prompt (last user message, within messages)
    - Actual top-level tool schemas
    - Safety/output reserves (applied by caller via _output_budget)

    Strategy:
    1. If we have a real baseline (last_real from previous response),
       estimate only the DELTA (new messages since last turn) and add to baseline.
    2. If no real data (cold start), fall back to full character-count estimation.

    This gives much more accurate estimates after the first response.

    Uncertainty guard (mode-aware):
    - exact: 0 additional (tokenizer is authoritative)
    - calibrated: proportional to tracker margin (cold start = larger)
    - shadow: 0 additional (admission uses exact count)

    Admission invariant: estimate_full_payload_tokens(...) + output_budget <= MAX_CONTEXT
    """
    # Get real baseline from tracker
    last_real = calibrator.get_last_real(session_key)

    # Detect stale baseline: if the current message count is LESS than what
    # was sent in the previous request, the window has been trimmed (old
    # messages dropped by build_context). In that case, last_real reflects
    # the OLD, larger request and is NOT a valid baseline for the current
    # (smaller) window. Fall back to cold-path full estimation.
    if last_real > 0:
        # Guard: poisoned baseline (real >= MAX_CONTEXT from a previous oversized request)
        if last_real >= MAX_CONTEXT:
            log.warning(
                "Poisoned baseline detected: last_real=%d >= MAX_CONTEXT=%d - using cold path",
                last_real, MAX_CONTEXT,
            )
            last_real = 0
        else:
            # Detect stale baseline: if the current message count is LESS than what
            # was sent in the previous request, the window has been trimmed
            prev_msg_count = calibrator.get_msg_count(session_key)
            if prev_msg_count > 0 and len(messages) < prev_msg_count:
                log.debug(
                    "Stale baseline detected: current_msgs=%d < prev_msgs=%d (window trimmed) - using cold path",
                    len(messages), prev_msg_count,
                )
                last_real = 0

    if last_real > 0:
        # Warm path: estimate only the delta
        # The delta is the new content added since the last response
        delta_tokens = _estimate_delta(messages)
        total = last_real + delta_tokens + note_tokens
    else:
        # Cold path: full estimation (same as before)
        msg_tokens = count_messages_tokens(messages)
        tools_tokens = count_tools_tokens(tools) if tools else 0
        template_overhead = len(messages) * 4
        total = msg_tokens + tools_tokens + template_overhead + note_tokens

    # Mode-aware uncertainty guard
    import proxy.app as _app
    _mode = getattr(_app, "ESTIMATOR_MODE", ESTIMATOR_MODE)
    if _mode == "exact":
        return total
    if _mode == "shadow":
        return total
    # Calibrated: apply confidence-based margin
    _, margin = calibrator.get_rho(session_key)
    return int(total * (1 + margin))


def _output_budget(input_tokens: int) -> int:
    """Single authoritative output budget: never exceed MAX_OUTPUT, and never go
    negative. The caller enforces the MAX_OUTPUT floor (via _emergency_shrink /
    a 413) before sending a starved request.

    Applies CTXGATE_INPUT_ESTIMATE_HEADROOM to proactively reduce max_tokens,
    absorbing estimator underestimation without waiting for a recut.

    The result is clamped to >= 0. Use _output_budget_checked() to distinguish
    "clamped to 0" from "genuinely 0".
    """
    effective_input = int(input_tokens * (1.0 + CTXGATE_INPUT_ESTIMATE_HEADROOM))
    if effective_input != input_tokens:
        log.debug(
            "Input headroom: raw=%d effective=%d (headroom=%.2f)",
            input_tokens, effective_input, CTXGATE_INPUT_ESTIMATE_HEADROOM,
        )
    raw = MAX_CONTEXT - effective_input - SAFETY_MARGIN - 1
    return max(0, min(MAX_OUTPUT, raw))


def _output_budget_checked(input_tokens: int) -> tuple:
    """Output budget with validity flag.

    Returns (budget, is_valid) where:
    - budget: the clamped output budget (always >= 0)
    - is_valid: False if the raw (pre-floor) value would be negative,
      meaning the input alone exceeds the usable context window.

    This lets callers distinguish "clamped to 0 because input is too large"
    from "genuinely 0 because MAX_OUTPUT is 0".
    """
    effective_input = int(input_tokens * (1.0 + CTXGATE_INPUT_ESTIMATE_HEADROOM))
    raw = MAX_CONTEXT - effective_input - SAFETY_MARGIN - 1
    is_valid = raw >= 0
    budget = max(0, min(MAX_OUTPUT, raw))
    return budget, is_valid
