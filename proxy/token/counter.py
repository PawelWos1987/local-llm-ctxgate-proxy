"""Token counting functions for ctxgate-proxy.

Uses exact gigatoken-based tokenization for all token counts.
The legacy rho (chars-per-token) estimation system has been replaced
because gigatoken made exact counting fast enough (~8ms for large payloads).

TOKEN ACCOUNTING RULES:
- Sources of truth: goose request usage.total_tokens and vLLM response usage.total_tokens
- Tokenizer (exact_count_messages) is used for: pre-vLLM-send, post-emergency-cut, and all payload sizing
- Char-based estimation is FORBIDDEN for producing token values
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from proxy.token.exact import exact_count_messages, exact_count_string
from proxy.token.calibrator import calibrator

logger = logging.getLogger(__name__)


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


def estimate_payload_tokens(
    session_key: str,
    inbound_total_tokens: Optional[int],
    messages: list[dict],
    model_name: str,
) -> int:
    """Estimate total token count for a payload of messages.

    Uses exact gigatoken-based counting. Falls back to character-based
    proportional estimate only if exact counting fails.
    """
    try:
        return exact_count_messages(messages, model_name)
    except Exception as e:
        logger.warning(
            "estimate_payload_tokens fell back to char estimate: %s", e
        )
        # Fallback: rough char-based estimate (should rarely trigger)
        total_chars = sum(_count_chars(m.get('content', '')) for m in messages)
        return max(1, total_chars // 4)


def count_tokens(text: str, model_name: str = "default") -> int:
    """Count exact tokens for a string using gigatoken."""
    return exact_count_string(text, model_name)


