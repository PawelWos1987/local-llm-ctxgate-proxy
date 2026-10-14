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

logger = logging.getLogger(__name__)


def count_tokens(text: str, model_name: str = "default") -> int:
    """Count exact tokens for a string using gigatoken."""
    return exact_count_string(text, model_name)
