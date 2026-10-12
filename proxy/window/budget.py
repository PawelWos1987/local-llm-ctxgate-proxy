"""Budget calculation: safe admitted budget, output budget, token estimation."""
import logging

from proxy.config.constants import MAX_INPUT, MAX_CONTEXT, SAFETY_MARGIN, MIN_OUTPUT, MAX_OUTPUT, ESTIMATOR_MODE, CTXGATE_INPUT_ESTIMATE_HEADROOM
from proxy.token.counter import count_messages_tokens, count_tools_tokens
from proxy.token.calibrator import calibrator

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


def _safe_admitted_budget() -> int:
    """The safe admitted input budget: the effective ceiling for input tokens."""
    return min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT)


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
    return count_messages_tokens(new_msgs, estimate=True)


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

    if last_real > 0:
        # Warm path: estimate only the delta
        # The delta is the new content added since the last response
        delta_tokens = _estimate_delta(messages)
        total = last_real + delta_tokens + note_tokens
    else:
        # Cold path: full estimation (same as before)
        msg_tokens = count_messages_tokens(messages, estimate=True)
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
    negative. The caller enforces the MIN_OUTPUT floor (via _emergency_shrink /
    a 413) before sending a starved request.

    Applies CTXGATE_INPUT_ESTIMATE_HEADROOM to proactively reduce max_tokens,
    absorbing estimator underestimation without waiting for a recut.
    """
    effective_input = int(input_tokens * (1.0 + CTXGATE_INPUT_ESTIMATE_HEADROOM))
    if effective_input != input_tokens:
        log.debug(
            "Input headroom: raw=%d effective=%d (headroom=%.2f)",
            input_tokens, effective_input, CTXGATE_INPUT_ESTIMATE_HEADROOM,
        )
    return min(MAX_OUTPUT, MAX_CONTEXT - effective_input - SAFETY_MARGIN - 1)  # -1: absolute floor for estimator off-by-one
