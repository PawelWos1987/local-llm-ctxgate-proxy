"""Token counting functions for ctxgate-proxy.

Provides O(1) calibrated token estimation and exact tokenizer-based
counting depending on CTXGATE_TOKEN_ESTIMATOR mode.
"""
from __future__ import annotations

import contextvars
import json
from typing import Any, Optional

from proxy.config.constants import (
    RHO_COLD,
    K_TOOLS_COLD,
    VLLM_MODEL,
)

# TODO(I4): replace with AppContext
# Shared state references - use late imports to avoid circular dependency
# and to respect monkeypatching in tests.

def _get_enc():
    """Get the tokenizer from the app module (late import to avoid circularity)."""
    from proxy import app
    return app.enc

def _get_metrics():
    """Get the metrics dict from the app module."""
    from proxy import app
    return app.metrics

def _get_estimator_mode():
    """Get the estimator mode from the app module (respects monkeypatching)."""
    from proxy import app
    return app.ESTIMATOR_MODE

_rho_var: contextvars.ContextVar = contextvars.ContextVar("ctxgate_rho", default=RHO_COLD)


def count_tokens(text: str, estimate: bool = False) -> int:
    """Count tokens in text.

    Mode behavior:
    - exact: uses the real tokenizer (authoritative, no uncertainty).
    - calibrated: uses len/rho prediction with cache for immutable content.
    - shadow: computes calibrated prediction for telemetry, but returns
      the exact count (so admission is always safe).

    No synchronous full-history tokenizer call on the calibrated hot path.
    """
    from proxy.token.cache import _token_cache

    enc = _get_enc()
    metrics = _get_metrics()
    mode = _get_estimator_mode()

    if mode == "shadow":
        # Shadow: compute calibrated for logging, return exact for admission
        _shadow_pred = max(1, int(len(text) / _rho_var.get()))
        if enc is not None:
            try:
                _exact = len(enc.encode(text).ids)
            except AttributeError:
                _exact = len(enc.encode(text))
            metrics["shadow_comparison"]["total"] += 1
            metrics["shadow_comparison"]["last_calibrated"] = _shadow_pred
            metrics["shadow_comparison"]["last_exact"] = _exact
            metrics["shadow_comparison"]["last_delta_pct"] = (
                (_shadow_pred - _exact) / _exact * 100 if _exact else 0.0)
            return _exact
        return _shadow_pred

    if mode == "exact" and enc is not None:
        cached = _token_cache.get(text, VLLM_MODEL)
        if cached is not None:
            return cached
        try:
            result = len(enc.encode(text).ids)
        except AttributeError:
            result = len(enc.encode(text))
        _token_cache.put(text, VLLM_MODEL, result)
        return result

    # Calibrated mode: pure len/rho arithmetic (O(1), no tokenizer, no cache).
    # Cache is NOT used here because the result depends on _rho_var (per-session).
    return max(1, int(len(text) / _rho_var.get()))


def count_message_tokens(msg: dict, estimate: bool = False) -> int:
    """Predict tokens for a single message using pure length arithmetic.

    No json.dumps, no encode, no join. O(1) per message.
    """
    total_chars = 0
    content = msg.get("content")
    if content:
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for p in content:
                if isinstance(p, dict):
                    total_chars += len(p.get("text", ""))
    total_chars += len(msg.get("role", "")) + 4
    for tc in msg.get("tool_calls", []):
        total_chars += len(tc.get("id", ""))
        fn = tc.get("function", {})
        total_chars += len(fn.get("name", ""))
        total_chars += len(fn.get("arguments", ""))
        total_chars += 10
    if msg.get("tool_call_id"):
        total_chars += len(msg["tool_call_id"])
    return max(1, int(total_chars / _rho_var.get()))


def count_messages_tokens(messages: list, estimate: bool = False) -> int:
    """Predict total tokens for a message list using pure length arithmetic.

    O(n) in the number of messages, zero tokenizer calls, no string
    concatenation of the full content.
    """
    if not messages:
        return 0
    total = 0
    for m in messages:
        total += count_message_tokens(m)
    return total


def count_tools_tokens(tools: list) -> int:
    """Predict tokens for a tools definition list."""
    if not tools:
        return 0
    total_chars = 0
    for t in tools:
        fn = t.get("function", {})
        total_chars += len(fn.get("name", ""))
        total_chars += len(fn.get("description", ""))
        params = fn.get("parameters", {})
        if isinstance(params, dict):
            total_chars += len(json.dumps(params, ensure_ascii=False, separators=(",", ":")))
        total_chars += 30
    return max(1, int(total_chars / K_TOOLS_COLD))
