"""Resume-time reconciliation: adapt stored session state to current limits.

Called on the first request for a session after a proxy restart (or when
limits have been tightened since the session was created). Computes the
projected context size including ALL proxy-injected entities and adapts
the window state to fit within the current budget.

Core invariant: after reconciliation, the projected context (window +
injection + tools + system) must fit within MAX_CONTEXT - MAX_OUTPUT -
SAFETY_MARGIN. If it cannot fit even after full adaptation, a
Goose-recoverable response is returned (never a human-only stop).
"""
import logging
import time
from typing import Optional

from proxy.config.constants import (
    MAX_CONTEXT, MAX_INPUT, SAFETY_MARGIN, MAX_OUTPUT,
    CTXGATE_INJECT_MAX_TOKENS_CFG,
)
from proxy.window.state import _new_window_state
from proxy.token.counter import count_messages_tokens

log = logging.getLogger("ctxgate-proxy")


def _projected_context_tokens(
    window_tokens: int,
    injection_budget: int,
    tool_tokens: int,
    system_tokens: int,
) -> int:
    """Compute the projected total context size including all proxy-injected entities.

    This is the SINGLE source of truth for resume-time budget decisions.
    The projection includes:
    - window_tokens: tokens in the rolling window (stored state)
    - injection_budget: max tokens the injection block will occupy
    - tool_tokens: tokens for tool schemas in the request
    - system_tokens: tokens for the system prompt (first message)
    """
    return window_tokens + injection_budget + tool_tokens + system_tokens


def _effective_budget() -> int:
    """The maximum allowed input tokens: MAX_CONTEXT - MAX_OUTPUT - SAFETY_MARGIN."""
    return MAX_CONTEXT - MAX_OUTPUT - SAFETY_MARGIN


def resume_reconcile(
    session_key: str,
    messages: list,
    body: dict,
    window_state: dict,
) -> tuple:
    """Reconcile stored session state with current limits.

    Called BEFORE the first turn is forwarded after a resume.
    Adapts the window state so that the projected context fits.

    Args:
        session_key: The session identifier.
        messages: The built list of message dicts to send.
        body: The raw request body dict (contains "tools", "model", etc.).
        window_state: The stored window state dict (may be None).

    Returns:
        Tuple of (adapted_messages, was_adapted, action):
        - adapted_messages: the (potentially modified) list of messages
        - was_adapted: bool, True if any adaptation was performed
        - action: str describing what was done ("none", "disable_thinking",
          "reduce_injection", "shrink_window", "reset")

    Strategy (in order of destructiveness):
    1. Disable thinking (free up thinking budget)
    2. Reduce injection budget
    3. Shrink the rolling window (re-cut)
    4. Reduce to minimum viable state (seed + 1 user message)

    Idempotent: safe to call multiple times. If the state already fits,
    returns it unchanged.
    """
    import proxy.app as _app

    # Handle None window_state
    if window_state is None:
        window_state = _new_window_state()

    # Extract tools from body
    tools = body.get("tools", [])

    # Compute current component sizes
    _injection_budget = CTXGATE_INJECT_MAX_TOKENS_CFG
    _tool_tokens = 0
    if tools:
        from proxy.token.counter import count_tools_tokens
        _tool_tokens = count_tools_tokens(tools)

    _system_tokens = 0
    if messages and messages[0].get("role") == "system":
        _system_tokens = count_messages_tokens([messages[0]])

    # Window tokens from stored state (approximate: use the stored token count
    # if available, otherwise estimate from the messages list)
    _window_tokens = window_state.get("last_token_count", 0)
    if _window_tokens == 0 and messages:
        # Estimate: count all non-system messages
        non_sys = [m for m in messages if m.get("role") != "system"]
        _window_tokens = count_messages_tokens(non_sys)

    _projected = _projected_context_tokens(
        _window_tokens, _injection_budget, _tool_tokens, _system_tokens
    )
    _budget = _effective_budget()

    if _projected <= _budget:
        log.debug(
            "Resume reconcile: session=%s projected=%d <= budget=%d - no action needed",
            session_key, _projected, _budget,
        )
        return (messages, False, "none")

    log.warning(
        "Resume reconcile: session=%s projected=%d > budget=%d - adapting",
        session_key, _projected, _budget,
    )

    # Step 1: Disable thinking
    _thinking_disabled = False
    _thinking_savings = 0
    if not window_state.get("thinking_disabled", False):
        _thinking_disabled = True
        # Thinking typically consumes ~50% of output budget when enabled.
        # Disabling it frees up that space for input.
        from proxy.config.constants import MAX_OUTPUT
        _thinking_savings = int(MAX_OUTPUT * 0.3)  # conservative estimate
        window_state["thinking_disabled"] = True
        _projected -= _thinking_savings
        log.info(
            "Resume reconcile: session=%s step=disable_thinking savings=%d projected=%d",
            session_key, _thinking_savings, _projected,
        )

    if _projected <= _budget:
        _persist_reconciled(session_key, window_state, _app)
        return (messages, True, "disable_thinking")

    # Step 2: Reduce injection budget
    _orig_inject = _injection_budget
    _injection_budget = max(500, _injection_budget // 2)
    _projected -= (_orig_inject - _injection_budget)
    window_state["injection_budget_override"] = _injection_budget
    log.info(
        "Resume reconcile: session=%s step=reduce_injection %d->%d projected=%d",
        session_key, _orig_inject, _injection_budget, _projected,
    )

    if _projected <= _budget:
        _persist_reconciled(session_key, window_state, _app)
        return (messages, True, "reduce_injection")

    # Step 3: Shrink the rolling window
    _safe_ceiling = MAX_INPUT
    if _window_tokens > _safe_ceiling:
        # Re-cut: keep only enough messages to fit the safe ceiling
        _current_cut = window_state.get("cut", 0)
        _total_msgs = window_state.get("total_msgs", len(messages))
        # Target: keep messages worth at most _safe_ceiling tokens
        _avg_msg_tokens = max(1, _window_tokens // max(1, _total_msgs - _current_cut))
        _msgs_to_keep = max(3, _safe_ceiling // _avg_msg_tokens)
        _new_cut = max(_current_cut, _total_msgs - _msgs_to_keep)
        window_state["cut"] = _new_cut
        _reduction = _window_tokens - (_msgs_to_keep * _avg_msg_tokens)
        _projected -= max(0, _reduction)
        _window_tokens = max(_safe_ceiling, _window_tokens - _reduction)
        log.info(
            "Resume reconcile: session=%s step=shrink_window cut=%d->%d projected=%d",
            session_key, _current_cut, _new_cut, _projected,
        )

    if _projected <= _budget:
        _persist_reconciled(session_key, window_state, _app)
        # Trim messages to match the new cut
        _new_cut = window_state.get("cut", 0)
        _sys_msgs = [m for m in messages if m.get("role") == "system"]
        _non_sys_msgs = [m for m in messages if m.get("role") != "system"]
        _adapted = _sys_msgs + _non_sys_msgs[_new_cut:]
        return (_adapted, True, "shrink_window")

    # Step 4: Minimum viable state (seed + 1 user message)
    # This is the last resort: keep only the system prompt + 1 user message.
    _min_viable = _system_tokens + 500 + _injection_budget + _tool_tokens
    if _min_viable <= _budget:
        window_state["cut"] = window_state.get("total_msgs", len(messages)) - 1
        window_state["thinking_disabled"] = True
        window_state["injection_budget_override"] = min(_injection_budget, 1000)
        _projected = _min_viable
        log.warning(
            "Resume reconcile: session=%s step=min_viable projected=%d budget=%d",
            session_key, _projected, _budget,
        )
        _persist_reconciled(session_key, window_state, _app)
        # Keep only system + last 1 non-system message
        _sys_msgs = [m for m in messages if m.get("role") == "system"]
        _non_sys_msgs = [m for m in messages if m.get("role") != "system"]
        _adapted = _sys_msgs + _non_sys_msgs[-1:]
        return (_adapted, True, "reset")

    # Even minimum viable state doesn't fit: this is a configuration error.
    # Log it clearly and return the state as-is (the handler will return
    # a Goose-recoverable 200 response).
    log.error(
        "Resume reconcile: session=%s CANNOT FIT even minimum viable state "
        "(projected=%d budget=%d). Returning Goose-recoverable response.",
        session_key, _projected, _budget,
    )
    window_state["reconcile_failed"] = True
    return (messages, True, "reset")


def _persist_reconciled(session_key: str, window_state: dict, _app) -> None:
    """Persist the reconciled window state."""
    from proxy.window.persist import _window_persist
    _window_persist(session_key, window_state)
    _app.session_compactions[session_key] = window_state
    log.info(
        "Resume reconcile: session=%s persisted new checkpoint "
        "(cut=%d thinking_disabled=%s injection_budget=%d)",
        session_key,
        window_state.get("cut", 0),
        window_state.get("thinking_disabled", False),
        window_state.get("injection_budget_override", CTXGATE_INJECT_MAX_TOKENS_CFG),
    )
