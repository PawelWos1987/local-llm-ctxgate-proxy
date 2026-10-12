"""Hot path package: context building, message handling, session resolution, tracking."""
from proxy.hotpath.context import build_context, _prep_messages, _normalize_system_messages
from proxy.hotpath.messaging import (
    _repair_dangling_tool_calls, _detect_loop, _classify_truncation,
    sanitize_tool_calls, strip_reasoning,
)
from proxy.hotpath.session import _get_goose_session_id, _get_goose_session_info, _resolve_goose_session
from proxy.hotpath.tracking import _track_session_tokens, _record_call, _fd_breakdown
from proxy.hotpath.injection import _apply_epoch_injection
from proxy.hotpath.session_id import _resolve_session_identity
from proxy.hotpath.vllm_body import _build_vllm_body
from proxy.hotpath.handler import chat_completions

__all__ = [
    "build_context", "_prep_messages", "_normalize_system_messages",
    "_repair_dangling_tool_calls", "_detect_loop", "_classify_truncation",
    "sanitize_tool_calls", "strip_reasoning",
    "_get_goose_session_id", "_get_goose_session_info", "_resolve_goose_session",
    "_track_session_tokens", "_record_call", "_fd_breakdown",
    "_apply_epoch_injection",
    "_resolve_session_identity", "_build_vllm_body",
    "chat_completions",
]
