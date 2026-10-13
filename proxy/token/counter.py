"""Token counting functions for ctxgate-proxy.

Provides O(1) calibrated token estimation and exact tokenizer-based
counting for the context window management pipeline.

TOKEN ACCOUNTING RULES:
- Only two sources of truth: goose request usage.total_tokens and vLLM response usage.total_tokens
- Tokenizer (exact_count_messages) is allowed only: (1) pre-vLLM-send, (2) post-emergency-cut
- Proportion formula exception: new_total = original_total * (chars_after / chars_before) for cut decisions
- Char-based estimation (len/N) is FORBIDDEN for producing token values
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from contextvars import ContextVar

from proxy.config.constants import (
    RHO_COLD,
    RHO_LO,
    RHO_HI,
)
from proxy.token.calibrator import calibrator

logger = logging.getLogger(__name__)

# ContextVar holding the current calibrated rho (chars per token)
# Set per-request by the calibrator; defaults to RHO_COLD for cold starts.
_rho_var: ContextVar[float] = ContextVar("_rho_var", default=RHO_COLD)

def _count_chars(text: Any) -> int:
    """Count characters in a message content field (handles str or list)."""
    if isinstance(text, str):
        return len(text)
    if isinstance(text, list):
        total = 0
        for part in text:
            if isinstance(part, dict):
                total += len(part.get("text", ""))
            elif isinstance(part, str):
                total += len(part)
        return total
    return 0

def _proportional_estimate(original_tokens: int, original_chars: int, new_chars: int) -> int:
    """Proportion formula exception: estimate tokens after a cut.

    Used ONCE at the cut decision point to enable the cut operation.
    Formula: new_total = original_total * (chars_after_cut / chars_before_cut)

    This is the ONLY sanctioned non-source-of-truth token value in the system.
    """
    if original_chars <= 0:
        return original_tokens
    return int(original_tokens * (new_chars / original_chars))

def estimate_payload_tokens(
    messages: list[dict],
    session_key: str,
    max_tokens: int = 0,
    system_prompt: str = "",
    inbound_total_tokens: Optional[int] = None,
) -> int:
    """Estimate total payload tokens for a vLLM request.

    Uses the real-token baseline from the last vLLM response plus a
    calibrated estimate of the delta (new messages since last response).

    Args:
        messages: The full message list to send
        session_key: Session identifier for the calibrator
        max_tokens: Requested max output tokens
        system_prompt: System prompt text (if separate from messages)
        inbound_total_tokens: Total tokens reported by goose in the request
            (source of truth #1). If provided, used directly for cold-start
            estimation instead of char-based heuristics.

    Returns:
        Estimated total prompt tokens (input + system + output budget)
    """
    # Get the real baseline from last vLLM response (source of truth #2)
    baseline = calibrator.get_last_real(session_key)

    # Delta estimation: use message count tracking to identify new content
    last_msg_count = calibrator.get_msg_count(session_key)
    if last_msg_count > 0 and last_msg_count < len(messages):
        # New messages exist since last observation
        delta_msgs = messages[last_msg_count:]
        # Use proportional estimate relative to known totals
        if inbound_total_tokens is not None:
            all_chars = sum(_count_chars(m.get("content", "")) for m in messages)
            delta_chars = sum(_count_chars(m.get("content", "")) for m in delta_msgs)
            delta = _proportional_estimate(inbound_total_tokens, all_chars, delta_chars)
        else:
            # No source of truth: cannot estimate delta reliably
            delta = 0
    elif last_msg_count > 0 and last_msg_count >= len(messages):
        # No new messages (or messages were trimmed) — delta is zero
        delta = 0
    else:
        # Cold start: no stored msg_count
        if inbound_total_tokens is not None:
            # Use goose-reported tokens as our best estimate
            delta = inbound_total_tokens
        else:
            # Cold start with no source of truth: cannot estimate reliably
            delta = 0

    # System prompt tokens (if provided separately)
    sys_tokens = 0  # System prompt tokens will come from real measurement (vLLM/goose)

    if baseline > 0:
        # We have real data: baseline + delta + system + output budget
        # The baseline already includes the system prompt from last time,
        # so we only add delta and any NEW system prompt changes
        total = baseline + delta + sys_tokens + max_tokens
    else:
        # Cold start: use inbound tokens (from goose) as the base
        base = delta if inbound_total_tokens is not None else delta
        total = base + sys_tokens + max_tokens

    # Apply confidence-based margin
    rho, margin = calibrator.get_rho(session_key)
    total = int(total * (1.0 + margin))

    return total

def observe_tokens(
    session_key: str,
    predicted: int,
    real_prompt_tokens: int,
    msg_count: int = 0,
) -> None:
    """Record a real token observation from vLLM response.

    Args:
        session_key: Session identifier
        predicted: Our estimated token count
        real_prompt_tokens: Actual prompt_tokens from vLLM usage
        msg_count: Number of messages in the request
    """
    calibrator.observe(session_key, predicted, real_prompt_tokens, msg_count)
