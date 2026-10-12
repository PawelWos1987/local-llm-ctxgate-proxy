"""vLLM request body construction from built context and client parameters."""
import logging

from proxy.config import (
    VLLM_MODEL, MIN_TEMPERATURE, PRESENCE_PENALTY,
    REPETITION_DETECTION, THINKING_TOKEN_BUDGET, CTXGATE_PREFIX_DIAG_ON,
)
from proxy.hotpath.context import _normalize_system_messages

log = logging.getLogger("ctxgate-proxy")


def _build_vllm_body(
    built: list, body: dict, max_tokens: int, stream: bool,
    client_cap: int | None, session_key: str,
    disable_thinking: bool = False,
) -> dict:
    """Construct the vLLM request body from the built context and client params.

    When disable_thinking is True, thinking_token_budget is forced to 0 so that
    all output tokens go to content (used as a corrective action when the
    output budget is too small to sustain both thinking and content).
    """
    vllm_body = {
        "model": VLLM_MODEL,
        "messages": built,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    # Sampling: send temperature ONLY when the client sent one and it is at/above
    # the floor. Otherwise omit it so the server's tuned default (1.0) applies.
    _ct = body.get("temperature")
    if _ct is not None:
        if _ct < 0:
            vllm_body["temperature"] = _ct
        elif _ct >= MIN_TEMPERATURE:
            vllm_body["temperature"] = _ct
        else:
            log.info("Dropping client temperature %s (< MIN_TEMPERATURE %s); server default 1.0 applies", _ct, MIN_TEMPERATURE)
    if PRESENCE_PENALTY:
        vllm_body["presence_penalty"] = float(PRESENCE_PENALTY)
    if REPETITION_DETECTION:
        vllm_body["repetition_detection"] = {"max_pattern_size": 50, "min_pattern_size": 5, "min_count": 6}
    if disable_thinking:
        vllm_body["thinking_token_budget"] = 0
    elif THINKING_TOKEN_BUDGET:
        vllm_body["thinking_token_budget"] = int(THINKING_TOKEN_BUDGET)
    # Forward optional vLLM passthrough parameters when the client
    # explicitly sent them AND we are on the capped fallback path.
    if client_cap is not None:
        _min_tok = body.get("min_tokens")
        if isinstance(_min_tok, int) and _min_tok > 0:
            vllm_body["min_tokens"] = min(_min_tok, max_tokens)
        if body.get("ignore_eos") is True:
            vllm_body["ignore_eos"] = True
    if stream:
        vllm_body["stream_options"] = {"include_usage": True}
    if body.get("tools"):
        vllm_body["tools"] = body["tools"]
    if body.get("tool_choice"):
        vllm_body["tool_choice"] = body["tool_choice"]
    vllm_body["messages"] = _normalize_system_messages(vllm_body.get("messages", []))
    if CTXGATE_PREFIX_DIAG_ON:
        _prefix_diag(session_key, vllm_body.get("messages", []))
    return vllm_body


def _prefix_diag(session_key: str, messages: list):
    """Debug: log prefix hash for cache diagnostics."""
    import hashlib
    prefix = "".join(m.get("content", "")[:100] for m in messages[:3])
    log.debug("prefix_diag session=%s hash=%s", session_key, hashlib.md5(prefix.encode()).hexdigest()[:8])
