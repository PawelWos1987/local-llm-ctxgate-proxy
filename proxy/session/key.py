"""Session key computation for ctxgate-proxy.

Provides functions to compute deterministic session identifiers
from message content and headers.
"""
from __future__ import annotations

import hashlib

from proxy.config.constants import CTXGATE_SESSION_IDENTITY


def _prefix_raw(messages: list) -> str:
    """Extract system + first user message content, joined with NUL separator."""
    parts = []
    for m in messages:
        role = m.get("role", "")
        if role == "system":
            c = m.get("content", "")
            if isinstance(c, list):
                c = ' '.join(p.get("text", "") for p in c if isinstance(p, dict))
            parts.append(c or "")
        elif role == "user" and len(parts) > 0:
            c = m.get("content", "")
            if isinstance(c, list):
                c = ' '.join(p.get("text", "") for p in c if isinstance(p, dict))
            parts.append(c or "")
            break
    return "\x00".join(parts)


def _fallback_fingerprint(messages: list) -> str:
    """sha256(system + \x00 + first_user)[:16]. First system
    message and first user message only; None if there is no
    user message at all."""
    sys_text = ""
    user_text = ""
    for m in messages:
        role = m.get("role", "")
        if role == "system" and not sys_text:
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    p.get("text", "") for p in c
                    if isinstance(p, dict))
            sys_text = c or ""
        elif role == "user" and not user_text:
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    p.get("text", "") for p in c
                    if isinstance(p, dict))
            user_text = c or ""
            if sys_text:
                break
    if not user_text:
        return ""
    return hashlib.sha256(
        (sys_text + "\x00" + user_text).encode()
    ).hexdigest()[:16]


def make_session_key(x_session_id: str, messages: list) -> str:
    """Compute a stable session key from the session ID and message prefix."""
    fp = hashlib.sha256(_prefix_raw(messages).encode()).hexdigest()[:8]
    return f"{x_session_id}:{fp}"


def _compute_x_sid(tenant_id: str, x_session_id_header: str, messages: list) -> str:
    """Compute x_sid based on CTXGATE_SESSION_IDENTITY mode.

    goose mode (default): Use X-Session-ID header if present, else content fingerprint.
    hash mode: Use sha256(header + \x00 + prefix_raw) for deterministic identity.
    """
    if CTXGATE_SESSION_IDENTITY == "hash":
        prefix_raw = _prefix_raw(messages)
        h = hashlib.sha256((x_session_id_header + "\x00" + prefix_raw).encode()).hexdigest()[:16]
        return f"{tenant_id}:{h}"
    # goose mode (default): preserve existing behavior
    if x_session_id_header:
        return f"{tenant_id}:{x_session_id_header}"
    # Fallback: fingerprint of first system + first user message
    sys_msg = ""
    user_msg = ""
    for m in messages:
        if m.get("role") == "system" and not sys_msg:
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            sys_msg = c
        elif m.get("role") == "user" and not user_msg:
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            user_msg = c
            break
    scope = hashlib.sha256((sys_msg + user_msg).encode()).hexdigest()[:16]
    return f"{tenant_id}:{scope}"
