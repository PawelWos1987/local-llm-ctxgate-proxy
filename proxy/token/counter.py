"""Token counting functions for ctxgate-proxy.

Provides O(1) calibrated token estimation and exact tokenizer-based
counting for the context window management pipeline.
"""
from __future__ import annotations

import logging
from typing import Any
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


def count_tokens(text: str) -> int:
    """Estimate token count for a plain string using chars/token ratio."""
    if not text:
        return 0
    return len(text) // RHO_COLD


def count_tools_tokens(tools: list[dict]) -> int:
    """Estimate token count for a list of tool definitions."""
    if not tools:
        return 0
    total_chars = 0
    for t in tools:
        func = t.get("function", {})
        total_chars += len(func.get("name", ""))
        total_chars += len(func.get("description", ""))
        params = func.get("parameters")
        if params:
            import json as _json
            total_chars += len(_json.dumps(params))
    return total_chars // RHO_COLD


def count_message_tokens(msg: dict) -> int:
    """Estimate token count for a single message using character heuristic.

    Uses chars/token ratio with a small per-message overhead for
    role markers and JSON structure.
    """
    if not isinstance(msg, dict):
        return 0
    chars = _count_chars(msg.get("content", ""))
    # Add overhead for role, name, tool_call_id fields
    overhead = 10  # approximate tokens for JSON structure
    # Add tool_calls overhead if present
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            func = tc.get("function", {})
            overhead += _count_chars(func.get("arguments", "")) // RHO_COLD + 5
    return (chars // RHO_COLD) + overhead


def count_messages_tokens(messages: list[dict]) -> int:
    """Estimate total token count for a list of messages."""
    return sum(count_message_tokens(m) for m in messages)


def _estimate_delta(messages: list[dict], session_key: str) -> int:
    """Estimate the token delta since the last vLLM response.

    Uses the stored message count to precisely identify which messages
    are new. This is critical for agentic loops where there is only a
    single user message but many assistant/tool messages accumulate
    between turns.

    Strategy:
    1. Get the message count from the last observation (msg_count)
    2. The delta is the sum of tokens for messages[msg_count:]
    3. If msg_count is 0 (cold start), fall back to estimating all
       non-first-user messages as delta

    This ensures we never re-count the entire conversation as "new",
    which would cause massive overestimation in long agentic sessions.
    """
    last_msg_count = calibrator.get_msg_count(session_key)

    if last_msg_count > 0 and last_msg_count < len(messages):
        # Precise delta: only count messages added since last observation
        delta_msgs = messages[last_msg_count:]
        return count_messages_tokens(delta_msgs)
    elif last_msg_count > 0 and last_msg_count >= len(messages):
        # No new messages (or messages were trimmed) — delta is zero
        return 0
    else:
        # Cold start: no stored msg_count. Estimate delta as all messages
        # except the first user message (which is assumed to be in the
        # baseline). This is conservative but safe.
        if len(messages) <= 1:
            return count_messages_tokens(messages)
        # Skip the first user message (assumed to be in baseline)
        first_user_idx = 0
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                first_user_idx = i
                break
        return count_messages_tokens(messages[first_user_idx + 1:])


def estimate_payload_tokens(
    messages: list[dict],
    session_key: str,
    max_tokens: int = 0,
    system_prompt: str = "",
) -> int:
    """Estimate total payload tokens for a vLLM request.

    Uses the real-token baseline from the last vLLM response plus a
    calibrated estimate of the delta (new messages since last response).

    This is far more accurate than estimating the entire conversation
    from scratch, especially in long agentic sessions.

    Args:
        messages: The full message list to send
        session_key: Session identifier for the calibrator
        max_tokens: Requested max output tokens
        system_prompt: System prompt text (if separate from messages)

    Returns:
        Estimated total prompt tokens (input + system + output budget)
    """
    # Get the real baseline from last vLLM response
    baseline = calibrator.get_last_real(session_key)

    # Estimate the delta (new messages since last observation)
    delta = _estimate_delta(messages, session_key)

    # System prompt tokens (if provided separately)
    sys_tokens = len(system_prompt) // RHO_COLD if system_prompt else 0

    if baseline > 0:
        # We have real data: baseline + delta + system + output budget
        # The baseline already includes the system prompt from last time,
        # so we only add delta and any NEW system prompt changes
        total = baseline + delta + sys_tokens + max_tokens
    else:
        # Cold start: estimate everything from scratch
        total = count_messages_tokens(messages) + sys_tokens + max_tokens

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

