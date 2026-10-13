"""Window management: state, cutting, shrinking, eliding, invariants."""

from proxy.window.state import (
    _norm_content, _msg_anchor, _seed_sig,
    _new_window_state, _window_valid, _window_epoch,
    _newest_user_idx, _strip_proxy_metadata,
)
from proxy.window.cut import (
    _eviction_trigger, _trim_target,
    _turn_groups, _safe_cut, _recut_to, _recut,
    _kept_messages, _is_context_length_error,
    _repair_tool_dependencies,
)
from proxy.window.budget import _output_budget, estimate_full_payload_tokens
from proxy.window.elide import _elided_tool_content, _stable_elide_inplace, _stable_elide_idx
from proxy.window.shrink import ContextCapacityError, _emergency_shrink, _protected_indices
from proxy.window.persist import (
    _window_persist, _window_load, _window_cleanup_ttl,
    invalidate_window_cache, clear_window_cache,
    _flush_pending_persists, _periodic_flush_loop,
)
from proxy.window.invariants import (
    verify_context_invariants, _validate_outgoing_payload,
    protected_tool_groups, _live_prompt_identity,
)

__all__ = [
    "_norm_content", "_msg_anchor", "_seed_sig",
    "_new_window_state", "_window_valid", "_window_epoch",
    "_newest_user_idx", "_strip_proxy_metadata",
    "_eviction_trigger", "_trim_target",
    "_turn_groups", "_safe_cut", "_recut_to", "_recut",
    "_kept_messages", "_is_context_length_error",
    "_repair_tool_dependencies",
    "_output_budget", "estimate_full_payload_tokens",
    "_elided_tool_content", "_stable_elide_inplace", "_stable_elide_idx",
    "ContextCapacityError", "_emergency_shrink", "_protected_indices",
    "_window_persist", "_window_load", "_window_cleanup_ttl",
    "invalidate_window_cache", "clear_window_cache",
    "_flush_pending_persists", "_periodic_flush_loop",
    "verify_context_invariants", "_validate_outgoing_payload",
    "protected_tool_groups", "_live_prompt_identity",
]
