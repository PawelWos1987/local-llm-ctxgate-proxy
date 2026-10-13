"""Budget calculation: output budget, token estimation."""
import logging

from proxy.config.constants import MAX_INPUT, MAX_CONTEXT, SAFETY_MARGIN, MAX_OUTPUT, ESTIMATOR_MODE
from proxy.token.counter import _count_chars, _proportional_estimate
from proxy.token.calibrator import calibrator

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


def _estimate_delta(messages: list, inbound_total_tokens: int | None = None) -> int:
    """Estimate tokens for NEW content only (since last real baseline).

    The last_real baseline includes all messages from the previous request.
    The new content is everything added AFTER the previous request's last
    user message (i.e., the assistant's response + any new user messages).

    Strategy:
    - Find the second-to-last user message (the one that triggered the
      previous response). Everything after it is new content.
    - If there's only one user message, we can't determine what's new,
      so return 0 (the baseline already covers it).

    When inbound_total_tokens is provided (from goose), uses the proportion
    formula to estimate the delta portion.
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
    
    if inbound_total_tokens is not None:
        # Proportion formula: estimate delta tokens from known total
        all_chars = sum(_count_chars(m.get("content", "")) for m in messages)
        new_chars = sum(_count_chars(m.get("content", "")) for m in new_msgs)
        return _proportional_estimate(inbound_total_tokens, all_chars, new_chars)
    
    # No source of truth available: cannot reliably estimate delta tokens.
    # Return 0 - the caller will rely on the next real measurement.
    return 0


def _output_budget(input_tokens: int) -> int:
    """Single authoritative output budget: never exceed MAX_OUTPUT, and never go
    negative. The caller enforces the MAX_OUTPUT floor (via _emergency_shrink /
    a 413) before sending a starved request.

    Applies to proactively reduce max_tokens,
    absorbing estimator underestimation without waiting for a recut.

    The result is clamped to >= 0. Use _output_budget_checked() to distinguish
    "clamped to 0" from "genuinely 0".
    """
    effective_input = input_tokens
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
    effective_input = input_tokens
    raw = MAX_CONTEXT - effective_input - SAFETY_MARGIN - 1
    is_valid = raw >= 0
    budget = max(0, min(MAX_OUTPUT, raw))
    return budget, is_valid
