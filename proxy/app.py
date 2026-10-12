import sys, os
# Prevent proxy/ dir from shadowing stdlib modules (e.g., token)
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir in sys.path:
    sys.path.remove(_script_dir)
# Add parent dir so "proxy" package is importable (append, not prepend)
sys.path.append(os.path.dirname(_script_dir))
# Register this module as "proxy.app" in sys.modules so that sub-modules
# importing "proxy.app" get the SAME module object (not a fresh copy with
# _vllm_client=None). This is needed because the systemd unit runs
# "python proxy/app.py" which loads this as __main__, not proxy.app.
if __name__ == "__main__":
    sys.modules["proxy.app"] = sys.modules[__name__]


# --- Re-exports: I1 (config, token, resilience, observability, session) ---
from proxy.config import *  # noqa: F401,F403
from proxy.config.env import _env_int, _env_float  # noqa: F401
from proxy.token import *  # noqa: F401,F403
from proxy.token.cache import _token_cache, _TokenCountCache, _TOKENIZER_VERSION, _TEMPLATE_VERSION, _SCHEMA_VERSION, _TOKEN_CACHE_MAX  # noqa: F401
from proxy.token.calibrator import calibrator  # noqa: F401
from proxy.resilience import *  # noqa: F401,F403
from proxy.observability import *  # noqa: F401,F403
from proxy.session import *  # noqa: F401,F403
from proxy.session.key import _fallback_fingerprint  # noqa: F401

# --- Re-exports: I2 (window, streaming, lm, db) ---
from proxy.window import *  # noqa: F401,F403
from proxy.streaming import *  # noqa: F401,F403
from proxy.lm import *  # noqa: F401,F403
from proxy.db import *  # noqa: F401,F403

# --- Re-exports: I3 (memory, knowledge, ledger, archive, summarize, auth, deliverables) ---
from proxy.memory import *  # noqa: F401,F403
from proxy.knowledge import *  # noqa: F401,F403
from proxy.ledger import *  # noqa: F401,F403
from proxy.archive import *  # noqa: F401,F403
from proxy.summarize import *  # noqa: F401,F403
from proxy.auth import *  # noqa: F401,F403
from proxy.auth.keys import _api_key_map  # noqa: F401
from proxy.deliverables import *  # noqa: F401,F403

# --- Re-exports: I4 (hotpath, lifecycle, utils, api) ---
from proxy.hotpath import *  # noqa: F401,F403
from proxy.lifecycle import *  # noqa: F401,F403
from proxy.utils import *  # noqa: F401,F403
from proxy.api import *  # noqa: F401,F403

# Explicit re-exports for underscore-prefixed names (star imports skip these)
from proxy.resilience.breaker import _CircuitBreaker, _BreakerOpen, _check_vllm_breaker, _check_lm_breaker, _vllm_breaker, _lm_breaker  # noqa: F401
from proxy.resilience.rate_limit import _TokenBucket, _get_tenant_rate_limiter, _get_tenant_semaphore  # noqa: F401
from proxy.window.state import _norm_content, _msg_anchor, _seed_sig, _newest_user_idx, _strip_proxy_metadata, _NOTE_TOK_CACHE  # noqa: F401
from proxy.window.cut import _eviction_trigger, _trim_target, _turn_groups, _safe_cut, _recut_to, _recut, _kept_messages, _is_context_length_error  # noqa: F401
from proxy.window.budget import _safe_admitted_budget, _output_budget, estimate_full_payload_tokens  # noqa: F401
from proxy.window.elide import _elided_tool_content, _stable_elide_inplace, _stable_elide_idx  # noqa: F401
from proxy.window.shrink import ContextCapacityError, _emergency_shrink, _protected_indices  # noqa: F401
from proxy.window.invariants import verify_context_invariants, _validate_outgoing_payload, _live_prompt_identity  # noqa: F401
from proxy.ledger.supersede import _entries_conflict, _apply_supersession  # noqa: F401
from proxy.ledger.format import _format_ledger_snapshot, _build_ledger_message  # noqa: F401
from proxy.archive.chunk import _chunk_tool_body, _retrieve_chunk_range, _deterministic_chunk_id  # noqa: F401
from proxy.archive.store import _store_tool_body, _retrieve_archived_content  # noqa: F401
from proxy.summarize.trim_summary import _summarize_trimmed_messages  # noqa: F401
from proxy.summarize.slices import _enqueue_context_slices, _fetch_session_summary  # noqa: F401
from proxy.token.counter import _rho_var  # noqa: F401
from proxy.hotpath.context import build_context, _prep_messages, _normalize_system_messages, _window_lock  # noqa: F401
from proxy.hotpath.messaging import _repair_dangling_tool_calls, _detect_loop, _classify_truncation, sanitize_tool_calls, strip_reasoning  # noqa: F401
from proxy.hotpath.session import _get_goose_session_id, _get_goose_session_info, _resolve_goose_session  # noqa: F401
from proxy.hotpath.tracking import _track_session_tokens, _record_call, _fd_breakdown  # noqa: F401
from proxy.hotpath.injection import _apply_epoch_injection  # noqa: F401
from proxy.hotpath.session_id import _resolve_session_identity  # noqa: F401
from proxy.hotpath.vllm_body import _build_vllm_body  # noqa: F401
from proxy.hotpath.handler import chat_completions  # noqa: F401
from proxy.lifecycle.lifespan import lifespan  # noqa: F401
from proxy.lifecycle.recovery import _spawn, _restart_recovery  # noqa: F401
from proxy.lifecycle.watchdog import _sd_notify, _watchdog_loop, _memory_worker_loop, _fd_hygiene_loop  # noqa: F401
from proxy.lifecycle.injection import _default_injection_metrics, _load_injection_metrics, _save_injection_metrics, _save_injection_metrics_async, _record_injection, _pct_str, _human_time  # noqa: F401
from proxy.utils.helpers import _clip_at_boundary, _fit_to_budget, _detect_recap_intent, _compute_epoch_key, _note_tok, _with_compaction_note, _eviction_priority, ctxgate_meta, explain_status, _newest_user_anchor  # noqa: F401
from proxy.utils.worker import _read_worker_status, _worker_pending_count, _evict_stale_sessions  # noqa: F401
from proxy.utils.vllm import _get_vllm_client, _make_vllm_client  # noqa: F401
from proxy.utils.checks import _check_config  # noqa: F401
from proxy.api.health import health, get_metrics, ready, metrics_prometheus  # noqa: F401
from proxy.api.admin import api_metrics, api_sessions, api_recent_calls, api_errors, api_memory_summary  # noqa: F401
from proxy.api.models import v1_models  # noqa: F401
from proxy.api.tokenize import tokenize_endpoint  # noqa: F401


"""local-llm-ctxgate-proxy: Context gate proxy for Goose -> vLLM with PG memory."""
import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import math
import os
import re
import contextvars
import uuid
import sys
import time
from collections import deque, OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiosqlite
import asyncpg
import httpx
import tiktoken
try:
    import tokenizers
except ImportError:
    tokenizers = None
from fastapi import FastAPI, Request, Response, Depends
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s [%(session_id)s] %(message)s")
log = logging.getLogger("ctxgate-proxy")

_session_ctx = {"id": "-"}

class _SessionFilter(logging.Filter):
    def filter(self, record):
        record.session_id = _session_ctx["id"]
        return True

log.addFilter(_SessionFilter())


# --- Runtime state (not in config) ---
SESSION_LAST_ACTIVE: dict[str, float] = {}
WORKER_PENDING_CACHE: dict = {"value": 0, "ts": 0.0}
WORKER_PENDING_TTL = 5.0

# --- Global state (per-session where applicable) ---
pool: Optional[asyncpg.Pool] = None
enc: Optional[Any] = None
sqlite_conn: Optional[aiosqlite.Connection] = None
sqlite_lock = asyncio.Lock()

# Per-session state, keyed by session_key = "{x_session_id}:{content_fp[:8]}"
_last_extract_anchor: dict[str, str] = {}
session_seeds: dict[str, list] = {}
_task_uuid_cache: dict[str, str] = {}
_ensure_task_done: set[str] = set()
_background_tasks: set[asyncio.Task] = set()
injection_metrics = _load_injection_metrics()
_goose_session_cache: dict[str, tuple] = {}
session_compactions: dict[str, dict] = {}
session_tokens: dict[str, dict] = {}
recent_calls: deque = deque(maxlen=200)
metrics: dict = {
    "requests_total": 0,
    "requests_ok": 0,
    "requests_error": 0,
    "trim_events": 0,
    "trim_sticky_reuse": 0,
    "window_collapse_guard": 0,
    "dropped_total": 0,
    "summary_slices_ok": 0,
    "summary_slices_failed": 0,
    "window_loads_db": 0,
    "window_persist_errors": 0,
    "compaction_events": 0,
    "toolcall_strips": 0,
    "tokens_in_total": 0,
    "tokens_out_total": 0,
    "max_context_seen": 0,
    "cached_tokens_total": 0,
    "prompt_tokens_total": 0,
    "tool_call_in_reasoning": 0,
    "evicted_sessions": 0,
    "extract_shed": 0,
    "extract_skipped_same_turn": 0,
    "extract_skipped_short": 0,
    "lm_tokens_by_kind": {
        "knowledge": {"prompt": 0, "completion": 0},
        "phase": {"prompt": 0, "completion": 0},
        "root": {"prompt": 0, "completion": 0},
        "other": {"prompt": 0, "completion": 0},
    },
    "lm_calls_by_kind": {
        "knowledge": 0, "phase": 0, "root": 0, "other": 0,
    },
    "recut_user_pinned": 0,
    "emergency_shrink_total": 0,
    "emergency_shrink_groups_dropped": 0,
    "inject_skipped_stale_user": 0,
    "started_at": time.time(),
    "output_total_budget_exhausted": 0,
    "output_truncated_total": 0,
    "output_truncated_by_reason": {},
    "output_continuations_total": 0,
    "output_max_tokens_seen": 0,
    "tool_call_truncated": 0,
    "tool_call_suppressed": 0,
    "tool_call_complete": 0,
    "unsafe_loop_blocked": 0,
    "recent_tool_preservation_failures": 0,
    "pinned_user_preservation_failures": 0,
    "root_summary_lag": 0,
    "summary_retry_count": 0,
    "memory_jobs_created": 0,
    "memory_jobs_dropped": 0,
    "memory_worker_lag": 0,
    "memory_store_success": 0,
    "memory_store_failure": 0,
    "memory_retrieval_hits": 0,
    "empty_after_retry": 0,
    "tool_args_unicode_escape": 0,
    "shadow_comparison": {"total": 0, "last_calibrated": 0, "last_exact": 0, "last_delta_pct": 0.0},
    "prefix_invalidations": 0,
    "requests_error_by_reason": {},
}
_lm_rate_limiter = None
_vllm_client = None
_lm_client = None
_lm_queue: "asyncio.PriorityQueue | None" = None
_lm_seq_counter = 0
_shutting_down = False
_inflight_count = 0

# --- Rolling-window persistence (P11) ---
LM_PRI_LOW = 2
_trim_summary_last: dict = {}
_window_persist_enabled: Optional[bool] = None
_window_persist_warned = False
_window_persist_cooldown_until: float = 0.0
_window_locks: dict = {}
_summary_task_locks: dict = {}
_summary_last_attempt: dict = {}
session_prefix_hashes: dict = {}
_window_load_tried: set = set()
_slice_log = []

# Compaction note appended to system message.
COMPACTION_NOTE = os.environ.get(
    "CTXGATE_COMPACTION_NOTE",
    "Older conversation turns may have been compacted. If a TASK STATE block "
    "appears in the newest user message, treat it as background only; the newest "
    "user message is the live instruction.")
_NOTE_TOK_CACHE: int | None = None

_DANGLING_PLACEHOLDER = "[earlier tool call archived]"
_EXTRACT_IN_FLIGHT = 0
_RESTART_RECOVERY_SQL = (
    "UPDATE proxy.summary_chunks "
    "SET state='queued', owner=NULL, lease_expires=NULL, updated_at=now() "
    "WHERE state IN ('claimed','processing') AND lease_expires < now()"
)

# Ledger cache
_ledger_cache = {}
_session_tenant: dict = {}


# --- FastAPI app ---
app = FastAPI(title="local-llm-ctxgate-proxy", version="1.0.0", lifespan=lifespan)


# --- Route registration ---
app.get("/health")(health)
app.get("/metrics", dependencies=[Depends(require_admin)])(get_metrics)
app.get("/ready")(ready)
app.get("/metrics/prometheus", dependencies=[Depends(require_admin)])(metrics_prometheus)
app.get("/api/metrics", dependencies=[Depends(require_admin)])(api_metrics)
app.get("/api/sessions", dependencies=[Depends(require_admin)])(api_sessions)
app.get("/api/recent-calls", dependencies=[Depends(require_admin)])(api_recent_calls)
app.get("/api/errors", dependencies=[Depends(require_admin)])(api_errors)
app.get("/api/memory-summary", dependencies=[Depends(require_admin)])(api_memory_summary)
app.get("/v1/models", dependencies=[Depends(require_auth)])(v1_models)
app.post("/v1/chat/completions", dependencies=[Depends(require_auth)])(chat_completions)
app.get("/tokenize")(tokenize_endpoint)

# Phase 2: /_test routes are only registered when CTXGATE_TEST_ENDPOINTS=1
if CTXGATE_TEST_ENDPOINTS:
    @app.post("/_test/reset_prefix")
    async def test_reset_prefix():
        return {"status": "reset"}

    @app.post("/_test/reset_sessions")
    async def test_reset_sessions():
        session_tokens.clear()
        recent_calls.clear()
        metrics["trim_events"] = 0
        metrics["compaction_events"] = 0
        metrics["trim_sticky_reuse"] = 0
        metrics["window_collapse_guard"] = 0
        metrics["dropped_total"] = 0
        metrics["summary_slices_ok"] = 0
        metrics["summary_slices_failed"] = 0
        metrics["window_loads_db"] = 0
        metrics["window_persist_errors"] = 0
        metrics["recut_user_pinned"] = 0
        metrics["inject_skipped_stale_user"] = 0
        return {"status": "reset", "cleared_sessions": 0}


# --- _check_tenant_rate_limit (kept here for backward compat) ---
def _check_tenant_rate_limit(tenant_id: str):
    """Check per-tenant rate limit. Returns a 429 JSONResponse if exceeded, else None."""
    if CTXGATE_TENANT_RATE_PER_SEC <= 0:
        return None
    tb = _get_tenant_rate_limiter(tenant_id)
    tb._refill()
    if tb.tokens < 1:
        deficit = 1 - tb.tokens
        retry_after = max(1, int(deficit / tb.rate))
        return JSONResponse(
            {"error": "rate limit exceeded", "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    tb.tokens -= 1
    return None


# --- _inject_ledger_into_context (kept here for backward compat) ---
def _inject_ledger_into_context(kept: list, task_uuid: str = None, session_key: str = "", tenant_id: str = "") -> list:
    """Inject the ledger snapshot as a separate system message into the context."""
    if not task_uuid and not session_key:
        return kept
    cached_entries = _ledger_cache.get((tenant_id, session_key) if session_key else (tenant_id, task_uuid))
    if not cached_entries:
        return kept
    ledger_msg = _build_ledger_message(cached_entries)
    if not ledger_msg:
        return kept
    insert_at = 0
    for i, m in enumerate(kept):
        if m.get("role") == "system":
            insert_at = i + 1
        else:
            break
    for m in kept:
        if m.get("role") == "system" and m.get("content", "").startswith("[LEDGER]"):
            return kept
    kept.insert(insert_at, ledger_msg)
    return kept


def _compute_gap_aware_watermark(chunks: list) -> int:
    """Compute the contiguous watermark: the end_idx of the last consecutively stored chunk."""
    if not chunks:
        return 0
    sorted_chunks = sorted(chunks, key=lambda c: c.get("chunk_seq", 0))
    watermark = 0
    for i, chunk in enumerate(sorted_chunks):
        if chunk.get("chunk_seq") != i:
            break
        if chunk.get("state") != "stored":
            break
        watermark = chunk.get("end_idx", 0)
    return watermark


def _sync_deliverable_summary(task_uuid: str, session_key: str = ""):
    """Sync deliverable summary to DB (fire-and-forget)."""
    from proxy.deliverables.sync import _sync_deliverable_summary as _sds
    return _sds(task_uuid, session_key)


if __name__ == "__main__":
    import socket as _sock
    import threading as _threading
    import sys as _sys
    import signal as _signal

    if "--check-config" in _sys.argv:
        _check_config()

    import uvicorn

    # Pre-flight config validation (exit 78 before binding port)
    _cfg_problems = validate_config()
    if _cfg_problems:
        sys.exit(78)

    # Port-lock guard
    _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    _s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    _port_free = False
    for _attempt in range(15):
        try:
            _s.bind(("127.0.0.1", PROXY_PORT))
            _s.close()
            _port_free = True
            break
        except OSError:
            if _attempt < 14:
                log.warning("Port %d still in use (attempt %d/15), waiting 1s...", PROXY_PORT, _attempt + 1)
                import time as _t; _t.sleep(1)
            else:
                log.error("Port %d in use after 15s. Another instance is running. Exiting(1).", PROXY_PORT)
                sys.exit(1)
    if not _port_free:
        sys.exit(1)

    def _global_asyncio_exception_handler(loop, context):
        log.warning("Unhandled asyncio exception: %s", context.get("exception") or context)
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.set_exception_handler(_global_asyncio_exception_handler)

    config = uvicorn.Config(app, host=CTXGATE_HOST, port=PROXY_PORT, log_level="warning", timeout_graceful_shutdown=3, limit_concurrency=100, timeout_keep_alive=65)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    import contextlib as _contextlib
    @ _contextlib.contextmanager
    def _noop_capture_signals():
        yield
    server.capture_signals = _noop_capture_signals

    _blocked_sigs = [_signal.SIGTERM, _signal.SIGINT, _signal.SIGHUP]
    _signal.pthread_sigmask(_signal.SIG_BLOCK, _blocked_sigs)
    log.info("blocked %s in main thread; sigwaitinfo watcher thread started", _blocked_sigs)

    def _sigwatcher(srv):
        _last = None
        while True:
            try:
                info = _signal.sigwaitinfo(frozenset(_blocked_sigs))
            except (KeyboardInterrupt, SystemExit):
                break
            except Exception as _e:
                log.warning("sigwaitinfo error: %s", _e)
                continue
            _now = time.monotonic()
            _sp = info.si_pid
            _cmdline = "?"
            try:
                with open("/proc/%d/cmdline" % _sp, "rb") as _cf:
                    _raw = _cf.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
                _cmdline = _raw if _raw else "(gone)"
            except OSError:
                _cmdline = "(process gone)"
            _chain = []
            _cur = _sp
            for _lvl in range(5):
                try:
                    with open("/proc/%d/status" % _cur) as _sf:
                        _st = _sf.read()
                except OSError:
                    break
                _pp = "?"
                for _ln in _st.splitlines():
                    if _ln.startswith("PPid:"):
                        _pp = _ln.split()[1]
                        break
                _nm = "?"
                for _ln in _st.splitlines():
                    if _ln.startswith("Name:"):
                        _nm = _ln.split()[1]
                        break
                _chain.append("%s(%d)" % (_nm, _cur))
                if _pp in ("?", "0") or int(_pp) <= 1:
                    break
                _cur = int(_pp)
            log.error(
                "SIGNAL RECEIVED signum=%d (%s) sender_pid=%d sender_uid=%d sender_cmdline=%r sender_ppid_chain=%s inflight=%d — draining in-flight requests before exit",
                info.si_signo, _signal.Signals(info.si_signo).name, _sp, info.si_uid, _cmdline, " > ".join(_chain) if _chain else "none", _inflight_count,
            )
            if _last is not None and (_now - _last) < 10.0:
                log.critical("second signal within 10s — forcing immediate exit(1)")
                os._exit(1)
            _last = _now
            srv.should_exit = True

    _watcher = _threading.Thread(target=_sigwatcher, args=(server,), name="ctxgate-sigwatcher", daemon=True)
    _watcher.start()

    server.run()
