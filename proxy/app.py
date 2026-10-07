"""local-llm-ctxgate-proxy: Context gate proxy for Goose -> vLLM with PG memory."""
import asyncio
import datetime
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiosqlite
import asyncpg
import httpx
import tiktoken
import tokenizers
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# --- Circuit breaker for backend connections ---
class _CircuitBreaker:
    """Opens after N consecutive failures, half-open probes after T seconds.
    When open, callers get an immediate _BreakerOpen exception (fail-fast)."""
    def __init__(self, name: str, threshold: int = 5, cooldown: float = 30.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._open_at = 0.0  # timestamp when breaker opened

    @property
    def is_open(self) -> bool:
        if self._failures < self.threshold:
            return False
        import time as _t
        return (_t.monotonic() - self._open_at) < self.cooldown

    @property
    def is_half_open(self) -> bool:
        if self._failures < self.threshold:
            return False
        import time as _t
        return (_t.monotonic() - self._open_at) >= self.cooldown

    def record_success(self):
        self._failures = 0
        self._open_at = 0.0

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.threshold:
            import time as _t
            self._open_at = _t.monotonic()
            log.warning("CircuitBreaker[%s] OPEN after %d consecutive failures (cooldown %.0fs)",
                        self.name, self._failures, self.cooldown)

    def allow(self) -> bool:
        """True if the request should proceed (closed or half-open)."""
        if self._failures < self.threshold:
            return True
        import time as _t
        return (_t.monotonic() - self._open_at) >= self.cooldown

class _BreakerOpen(Exception):
    pass

_vllm_breaker = _CircuitBreaker("vllm", threshold=5, cooldown=30.0)
_lm_breaker = _CircuitBreaker("lm", threshold=5, cooldown=30.0)

def _check_vllm_breaker():
    """Raise _BreakerOpen if the vLLM breaker is open."""
    if not _vllm_breaker.allow():
        raise _BreakerOpen(f"vLLM breaker open ({_vllm_breaker._failures} consecutive failures, cooldown {_vllm_breaker.cooldown}s)")

def _check_lm_breaker():
    """Raise _BreakerOpen if the LM breaker is open."""
    if not _lm_breaker.allow():
        raise _BreakerOpen(f"LM breaker open ({_lm_breaker._failures} consecutive failures, cooldown {_lm_breaker.cooldown}s)")


class _TokenBucket:
    """Classic token bucket rate limiter. Single event loop = no locking needed."""

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now

    async def acquire(self, amount: int = 1):
        while True:
            self._refill()
            if self.tokens >= amount:
                self.tokens -= amount
                return
            deficit = amount - self.tokens
            await asyncio.sleep(deficit / self.rate)


class MistralRateLimiter:
    """Enforces Mistral API limits: 500K TPM, 16.67 RPS.

    Single shared instance. All workers call acquire() before the HTTP
    request and release() after. The only component that knows the limits.
    """

    RPS_LIMIT = 16.67
    RPS_BURST = 16
    TPM_LIMIT = 500_000
    TPM_RATE = TPM_LIMIT / 60.0
    TPM_BURST = 500_000
    MAX_CONCURRENT = 10

    def __init__(self):
        self._rps = _TokenBucket(self.RPS_LIMIT, self.RPS_BURST)
        self._tpm = _TokenBucket(self.TPM_RATE, self.TPM_BURST)
        self._sem = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._active = 0
        self._total_acquired = 0
        self._total_waited = 0.0

    async def acquire(self, estimated_tokens: int = 2000) -> float:
        t0 = time.monotonic()
        await self._rps.acquire(1)
        await self._tpm.acquire(estimated_tokens)
        await self._sem.acquire()
        self._active += 1
        self._total_acquired += 1
        wait = time.monotonic() - t0
        self._total_waited += wait
        if wait > 2.0:
            log.warning("RateLimiter: waited %.1fs for slot (active=%d, rps_avail=%.1f, tpm_avail=%d)",
                        wait, self._active, self._rps.tokens,
                        int(self._tpm.burst - self._tpm.tokens))
        return wait

    def release(self, actual_tokens: int = 0, estimated_tokens: int = 2000):
        self._active -= 1
        self._sem.release()
        if actual_tokens and actual_tokens < estimated_tokens:
            self._tpm.tokens = min(self._tpm.burst, self._tpm.tokens + (estimated_tokens - actual_tokens))

    @property
    def stats(self) -> dict:
        return {
            "active": self._active,
            "rps_available": round(self._rps.tokens, 1),
            "tpm_available": int(self._tpm.tokens),
            "total_acquired": self._total_acquired,
            "avg_wait_s": round(self._total_waited / max(1, self._total_acquired), 3),
        }

# Single global instance
_lm_rate_limiter = MistralRateLimiter()

log = logging.getLogger("ctxgate-proxy")

def _load_dotenv() -> None:
    """Load .env from the project root (parent of proxy/). Real environment
    variables always take precedence over .env values (no overwrite).
    Dependency-free: a minimal KEY=VALUE parser is enough for this file."""
    import pathlib
    for cand in (pathlib.Path(__file__).resolve().parent.parent / ".env",
                 pathlib.Path.cwd() / ".env"):
        if cand.is_file():
            for line in cand.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    os.environ.setdefault(k, v)
            log.info("loaded environment from %s", cand)
            return
    log.warning("no .env file found (checked project root and cwd); using process environment only")

_load_dotenv()

import signal as _signal
import os as _os

import socket as _socket

def _sd_notify(msg: str) -> None:
    """Send an sd_notify message to systemd if NOTIFY_SOCKET is set."""
    addr = _os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        s.connect(addr)
        s.sendall(msg.encode())
        s.close()
    except OSError:
        pass

async def _watchdog_loop():
    """Ping systemd every 10s. WatchdogSec=60 in the unit means the proxy
    is killed and restarted if the event loop cannot schedule this task
    for 60 seconds — that is the deadlock detector."""
    while True:
        try:
            _sd_notify("WATCHDOG=1")
        except Exception as _e:
            log.warning("watchdog ping failed: %s", _e)
        await asyncio.sleep(10)

NL = "\n"  # newline constant for string building

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError) as e:
        log.warning("env %s=%r is not a valid int; using default %d (%s)", name, raw, default, e)
        return default

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError) as e:
        log.warning("env %s=%r is not a valid float; using default %s (%s)", name, raw, default, e)
        return default


MISTRAL_URL = os.environ.get("CTXGATE_LM_URL", "https://api.mistral.ai/v1").removesuffix("/chat/completions")
MISTRAL_MODEL = os.environ.get("CTXGATE_LM_MODEL", "mistral-small-latest")
MISTRAL_TIMEOUT = _env_int("CTXGATE_LM_TIMEOUT", 120)
MISTRAL_API_KEY = os.environ.get("CTXGATE_LM_API_KEY", "")

MISTRAL_SYSTEM_PROMPT = """You are the durable-memory worker for a long-running software engineering agent.

Your only job is to extract and maintain information that the main 27B agent will need after the current conversation context is no longer available.

Read the current task state and the new event. Preserve only durable, useful information: confirmed decisions, important findings, failed approaches, constraints, important TODOs, important files, state changes, and stable facts. Do not solve the task, do not execute tools, do not invent information, and do not repeat information that is already known unless the new event corrects or supersedes it.

Prefer precise factual statements over summaries or explanations. Never guess. Only use information explicitly present in the input. When a new fact contradicts an existing memory, mark the old information as superseded through the requested memory action. When nothing important changed, return no memory changes.

Return ONLY valid JSON matching this exact schema. No Markdown. No commentary. No explanation outside the JSON.

Schema:
{
  "memory_actions": [
    {
      "action": "NEW" | "UPDATE" | "SUPERSEDE" | "DUPLICATE" | "NO_CHANGE",
      "type": "DECISION" | "FINDING" | "FAILURE" | "TODO" | "CONSTRAINT" | "FILE" | "STATE" | "FACT",
      "importance": "CRITICAL" | "HIGH" | "NORMAL" | "LOW",
      "title": "short title",
      "content": "the fact",
      "source_event_id": "string"
    }
  ],
  "state_update": {
    "changed": true | false,
    "current_state": "brief current state" | null,
    "current_subtask": "current subtask" | null
  }
}"""

# --- Mistral priority queue (2 parallel consumers) ---
# Mistral API supports 16 req/s. Two consumers pull from the same priority queue: P0=trim, P2=knowledge.
_lm_queue: "asyncio.PriorityQueue | None" = None
_lm_client: "httpx.AsyncClient | None" = None
_lm_seq_counter = 0
LM_PRI_HIGH = 0   # trim summaries (feeds working memory into prompt)
LM_PRI_LOW = 2    # knowledge extraction (can wait 1 hour)
_trim_summary_last: dict = {}  # task_uuid -> timestamp of last stored summary
# --- Rolling-window persistence (P11) ---
_window_persist_enabled: Optional[bool] = None  # None=undecided, True/False after feature-detect
_window_persist_warned = False
_window_persist_cooldown_until: float = 0.0  # monotonic timestamp; skip writes until this time
_window_locks: dict = {}  # session_key -> asyncio.Lock (decide-and-persist is serialized)
_summary_task_locks: dict = {}  # task_uuid -> asyncio.Lock (slices run strictly in order)
_summary_last_attempt: dict = {}  # task_uuid -> (timestamp, cut_at_attempt)
session_prefix_hashes: dict = {}  # session_key -> [sha1 per message] from previous request
_window_load_tried: set = set()  # session_keys for which a lazy DB load was already attempted
_slice_log = []  # (start, end) of each summarized slice; capped; used for exactly-once verification
GOOSE_SESSIONS_DB = os.environ.get("GOOSE_SESSIONS_DB") or os.path.join(
    os.path.expanduser("~"), ".local", "share", "goose", "sessions", "sessions.db")

# === 4-Step Orchestrator Prompts ===

_PROMPT_EXTRACT = (
    "Extract concrete facts from the text below. "
    "Output JSON only with this structure: "
    "{\"state_update\": {\"changed\": true/false, \"current_state\": \"brief state\", \"current_subtask\": \"current task\"}, "
    "\"memory_actions\": [{\"action\": \"NEW\", \"type\": \"FACT\", \"importance\": \"NORMAL\", \"title\": \"short title\", \"content\": \"the fact\"}]} "
    "Extract: project names, tech stack, file paths, decisions, constraints, errors, config values, user preferences. "
    "Only extract what is explicitly stated. Never invent. If no new facts, use empty array for memory_actions."
)

_PROMPT_QUALITY = (
    "Review the proposed memory extraction. Check for hallucination (facts not in the original event) or broken JSON format. "
    "If ok, return: {\"pass\": true, \"reason\": \"clean\"} "
    "If hallucinated or malformed, return: {\"pass\": false, \"reason\": \"why\"} "
    "Output JSON only."
)

_PROMPT_DECIDE = (
    "Should these extracted memories be stored? They come from a real conversation event. "
    "If the facts are concrete and stated in the event: {\"store\": true} "
    "If empty, meaningless, or fully duplicated: {\"store\": false, \"reason\": \"why\"} "
    "Prefer storing. Output JSON only."
)

_IMPORTANCE_MAP = {"CRITICAL": 10, "HIGH": 8, "NORMAL": 5, "LOW": 3}


async def _lm_do_call(task: dict):
    """Execute a single Mistral API call with rate limiting."""
    global _lm_client
    messages = list(task["messages"])
    if task.get("system"):
        messages.insert(0, {"role": "system", "content": task["system"]})

    prompt_text = " ".join(
        m.get("content", "") if isinstance(m.get("content", ""), str) else str(m.get("content", ""))
        for m in messages
    )
    estimated_tokens = count_tokens(prompt_text) + task["max_tokens"]

    await _lm_rate_limiter.acquire(estimated_tokens)
    _check_lm_breaker()

    t0 = time.monotonic()
    body = {
        "model": MISTRAL_MODEL,
        "messages": messages,
        "max_tokens": task["max_tokens"],
        "temperature": task["temperature"],
    }
    try:
        resp = await _lm_client.post(MISTRAL_URL + "/chat/completions", json=body)
        _lm_breaker.record_success()
    except (httpx.ConnectError, httpx.TransportError) as e:
        _lm_breaker.record_failure()
        _lm_rate_limiter.release()
        raise

    data = None
    actual_tokens = 0
    if resp.status_code == 200:
        try:
            data = resp.json()
            actual_tokens = data.get("usage", {}).get("total_tokens", 0)
            # Accumulate token usage per call kind
            _kind = task.get("kind", "other")
            if _kind not in metrics["lm_tokens_by_kind"]:
                metrics["lm_tokens_by_kind"][_kind] = {"prompt": 0, "completion": 0}
                metrics["lm_calls_by_kind"][_kind] = 0
            metrics["lm_calls_by_kind"][_kind] += 1
            _usage = data.get("usage", {})
            metrics["lm_tokens_by_kind"][_kind]["prompt"] += _usage.get("prompt_tokens", 0)
            metrics["lm_tokens_by_kind"][_kind]["completion"] += _usage.get("completion_tokens", 0)
        except Exception:
            pass
    _lm_rate_limiter.release(actual_tokens, estimated_tokens)

    dt = time.monotonic() - t0
    if resp.status_code == 429:
        log.warning("Mistral rate limited (%.1fs): %s", dt, resp.text[:200])
        return {} if task["json_mode"] else ""
    if resp.status_code != 200:
        log.warning("Mistral API error %d (%.1fs): %s", resp.status_code, dt, resp.text[:200])
        return {} if task["json_mode"] else ""

    msg = data.get("choices", [{}])[0].get("message", {})
    content = msg.get("content", "")
    if not content:
        return {} if task["json_mode"] else ""
    if not task["json_mode"]:
        return content.strip()
    content = content.strip()
    if content.startswith("```"):
        segs = content.split("\n")
        content = "\n".join(segs[1:])
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return json.loads(content)


async def _lm_consumer(worker_id: int = 0):
    """Independent worker: pull task, execute, resolve future. No cross-blocking."""
    global _lm_client
    while True:
        priority, seq, task = await _lm_queue.get()
        # Time-based aging: P2 tasks waiting > 120s bump to priority 1
        if priority == LM_PRI_LOW:
            age = time.time() - task.get("enqueued_at", 0)
            if age > 120:
                priority = 1
        try:
            result = await _lm_do_call(task)
            if not task["future"].done():
                task["future"].set_result(result)
        except asyncio.CancelledError:
            if not task["future"].done():
                task["future"].cancel()
            raise
        except Exception as e:
            log.warning("Mistral worker-%d task failed (pri=%d seq=%d): %s",
                        worker_id, priority, seq, e)
            if not task["future"].done():
                task["future"].set_result({} if task["json_mode"] else "")
        finally:
            _lm_queue.task_done()

async def _call_4b(messages, max_tokens=2000, json_mode=True, priority=LM_PRI_HIGH, system=None, kind="other"):
    """Submit a Mistral API call to the priority queue. Awaits the result.

    Priority: LM_PRI_HIGH (0) for trim summaries, LM_PRI_LOW (2) for knowledge.
    Uses a shared httpx client and 2 consumer tasks, so Mistral API
    is never hit with more than 2 concurrent requests.
    """
    if _lm_queue is None or _lm_client is None:
        log.warning("Mistral queue not ready - _call_4b skipped")
        return {}
    global _lm_seq_counter
    _lm_seq_counter += 1
    future = asyncio.get_event_loop().create_future()
    task = {
        "priority": priority,
        "seq": _lm_seq_counter,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "json_mode": json_mode,
        "system": system,
        "kind": kind,
        "future": future,
        "enqueued_at": time.time(),
    }
    await _lm_queue.put((priority, _lm_seq_counter, task))
    try:
        return await asyncio.wait_for(future, timeout=300)
    except asyncio.TimeoutError:
        log.warning("Mistral queue call timed out after 300s (pri=%d)", priority)
        return {}
def _is_near_duplicate(existing_value: str, new_value: str) -> bool:
    """Check if two memory values are near-duplicates using token overlap."""
    stop = {"the","and","for","with","this","that","from","have","will","your","what",
            "when","where","which","how","can","could","would","should","about","there",
            "here","been","being","were","was","are","is","not","all","any","but","its",
            "you","our","their","then","than","into","over","under","also","just"}
    def _sig_tokens(text):
        return set(w for w in _re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{3,}', text.lower()) if w not in stop)
    ex_toks = _sig_tokens(existing_value)
    new_toks = _sig_tokens(new_value)
    if not ex_toks or not new_toks: return False
    overlap = len(ex_toks & new_toks)
    return overlap >= max(2, int(0.7 * min(len(ex_toks), len(new_toks))))

async def _memory_worker_loop():
    """DEPRECATED: Memory extraction is handled by worker/worker.py (dedicated process).
    This loop is kept as a no-op to avoid racing with the dedicated worker.
    The dedicated worker uses FOR UPDATE SKIP LOCKED for safe concurrent access.
    """
    log.info("Memory worker loop: DISABLED (dedicated worker.py handles extraction + recovery)")
    # A1: The worker owns stuck-job recovery (heartbeat + recover_stuck_jobs).
    # The proxy no longer marks jobs as failed - that was racing with the worker's
    # legitimate 3-4 sequential LLM calls (300s timeouts + 429 backoff).
    while True:
        await asyncio.sleep(60)


async def _summarize_trimmed_messages(task_uuid, session_key, rest, session_name: str = "", pending_cut: int = None):
    """Exactly-once, restart-safe summarization of the dropped region.

    The slice is ALWAYS [summarized_through, dropped_total) re-derived from the
    current in-memory window state (Goose resends the full history, so rest is
    always complete). Both bounds are monotonic, so the tiling has no gaps and no
    overlaps: every dropped message is summarized exactly once, in order.
    On success the watermark advances to dropped_total and is persisted; on
    failure it is left unchanged (WARNING) so the slice is re-derived and retried
    on a later request. A brand-new / pre-change session (no watermark) backfills
    only the NEWEST SUMMARY_BACKFILL_CHARS of the dropped region, in order.
    """
    global _summary_last_attempt
    if not pool or not rest:
        return
    lock = _summary_task_locks.get(task_uuid)
    if lock is None:
        lock = asyncio.Lock()
        _summary_task_locks[task_uuid] = lock
    async with lock:
        ws = session_compactions.get(session_key)
        if ws is None:
            return
        try:
            start_idx = ws.get("summarized_through", 0)
            end_idx = ws.get("dropped_total", ws.get("cut", 0))
            if pending_cut is not None:
                end_idx = min(end_idx, pending_cut)
            _slice_log.append((start_idx, end_idx))
            if len(_slice_log) > 1000:
                _slice_log[:] = _slice_log[-500:]
            _summary_last_attempt[task_uuid] = (time.time(), end_idx)
            if end_idx <= start_idx:
                return  # nothing new to summarize
            slice_msgs = rest[start_idx:end_idx]
            if not slice_msgs:
                return

            def _mtext(m):
                c = m.get("content", "")
                if isinstance(c, list):
                    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                return c or ""

            def _role_capped_text(m):
                """Role-aware text for the summarizer chunk. Caps tool output
                and assistant tool_call args; leaves user/assistant text alone.
                Never mutates the real message dict."""
                role = m.get("role", "")
                t = _mtext(m)
                if role == "tool" and SUMMARY_TOOL_CAP_CHARS > 0 and len(t) > SUMMARY_TOOL_CAP_CHARS:
                    omitted = len(t) - 1000
                    t = t[:600] + "[..." + str(omitted) + " chars omitted...]" + t[-400:]
                if role == "assistant":
                    tcs = m.get("tool_calls")
                    if tcs:
                        parts = []
                        for tc in tcs:
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            if isinstance(args, dict):
                                args = json.dumps(args)
                            if len(args) > 300:
                                args = args[:300] + "..."
                            parts.append("[tool_call:" + name + "(" + args + ")]")
                        if parts:
                            t = t + " " + " ".join(parts)
                return t

            total_chars = sum(len(_mtext(m)) for m in slice_msgs)
            if total_chars > SUMMARY_BACKFILL_CHARS:
                acc = 0
                keep_from = len(slice_msgs)
                for i in range(len(slice_msgs) - 1, -1, -1):
                    acc += len(_mtext(slice_msgs[i]))
                    keep_from = i
                    if acc >= SUMMARY_BACKFILL_CHARS:
                        break
                skipped = keep_from
                slice_msgs = slice_msgs[keep_from:]
                start_idx = start_idx + skipped
                log.info("backfill capped: skipped %d older messages (newest %d kept)", skipped, len(slice_msgs))
                if not slice_msgs:
                    return

            per_cap = 500
            def _build_chunks(cap):
                chunks = []
                cur = []
                cur_len = 0
                for m in slice_msgs:
                    t = _role_capped_text(m)
                    # A tool message already role-capped to SUMMARY_TOOL_CAP_CHARS is
                    # left as-is (the role cap is authoritative for tool output); the
                    # generic per-message cap only applies to non-role-capped text.
                    _role_capped = (m.get("role") == "tool" and SUMMARY_TOOL_CAP_CHARS > 0
                                    and len(_mtext(m)) > SUMMARY_TOOL_CAP_CHARS)
                    # BUG 3: keep head (60%) + tail (40%) of the per-message cap
                    # so tool outputs' results (usually at the tail) are not lost.
                    if not _role_capped and len(t) > cap:
                        _head = int(cap * 0.6)
                        _tail = cap - _head
                        t = t[:_head] + " ...[truncated]... " + t[-_tail:]
                    line = str(m.get("role", "?")) + ": " + t
                    if not line.strip():
                        continue
                    if cur and cur_len + len(line) + 1 > SUMMARY_CHUNK_CHARS:
                        chunks.append("\n".join(cur))
                        cur = []
                        cur_len = 0
                    cur.append(line)
                    cur_len += len(line) + 1
                if cur:
                    chunks.append("\n".join(cur))
                return chunks
            chunks = _build_chunks(per_cap)
            if len(chunks) > SUMMARY_MAX_CHUNKS:
                per_cap = 300
                chunks = _build_chunks(per_cap)
            if len(chunks) > SUMMARY_MAX_CHUNKS:
                per_cap = 200
                chunks = _build_chunks(per_cap)
            if not chunks:
                return

            last_phase_row = await pool.fetchrow("SELECT MAX(phase_number) as mp FROM proxy.phase_summaries WHERE task_id=$1", task_uuid)
            last_phase = (last_phase_row["mp"] or 0) if last_phase_row else 0
            # BUG 4: the phase prompt now requires explicit, labeled task-state
            # fields so the model retains task progress across a trim.
            phase_prompt = ("You are a conversation phase summarizer. Read the new messages and produce a concise summary of the task state after this phase. "
                           "Write it in PLAIN TEXT (no markdown) with EXACTLY these labeled fields, in this order: "
                           "'CURRENT TASK/PHASE: ' (what the agent is working on now); "
                           "'COMPLETED: ' (what is done, WITH evidence - name the reports/files and test results that prove it); "
                           "'IN PROGRESS: ' (what is actively being worked); "
                           "'NEXT STEP: ' (the single concrete next action); "
                           "'DECISIONS/CONSTRAINTS: ' (decisions made and constraints that must hold); "
                           "'DO NOT REDO: ' (work already finished that must not be repeated). "
                           "Be precise and factual. No commentary. At most 400 words.")
            async def _do_chunk(ci: int, chunk: str):
                new_phase = last_phase + 1 + ci
                session_tag = (" [Session: " + session_name + "]") if session_name else ""
                phase_user_msg = "Phase " + str(new_phase) + session_tag + ":\n" + chunk
                phase_result = await _call_4b(
                    [{"role": "user", "content": phase_user_msg}],
                    max_tokens=800, json_mode=False, system=phase_prompt,
                    kind="phase",
                )
                phase_summary = ""
                if isinstance(phase_result, str) and phase_result.strip():
                    phase_summary = phase_result.strip()
                elif isinstance(phase_result, dict):
                    su = phase_result.get("state_update", {})
                    if isinstance(su, dict) and su.get("current_state"):
                        phase_summary = su["current_state"].strip()
                if not phase_summary:
                    log.warning("Phase %d summarization: no usable text (chunk %d/%d)", new_phase, ci + 1, len(chunks))
                    return None
                await pool.execute(
                    "INSERT INTO proxy.phase_summaries (task_id, session_key, phase_number, summary, trimmed_msg_count, trimmed_tokens) "
                    "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (task_id, phase_number) DO UPDATE SET summary=$4, trimmed_msg_count=$5, trimmed_tokens=$6",
                    task_uuid, session_key, new_phase, phase_summary[:3000], len(slice_msgs), count_tokens(chunk),
                )
                metrics["summary_slices_ok"] += 1
                log.info("Phase %d summary stored: %d msgs, %d tokens, %d chars", new_phase, len(slice_msgs), count_tokens(chunk), len(phase_summary))
                return phase_summary

            chunk_results = await asyncio.gather(
                *[_do_chunk(ci, chunk) for ci, chunk in enumerate(chunks)],
                return_exceptions=True,
            )
            for ci, r in enumerate(chunk_results):
                if isinstance(r, Exception):
                    log.warning("Phase chunk %d/%d failed: %s", ci + 1, len(chunks), r)
            # BUG 1: stored_any was never set True (root cause of the stuck
            # watermark / infinite re-summarization). Derive it from the gather
            # results: any chunk that returned a non-empty string stored a phase.
            stored_any = any(isinstance(r, str) and r for r in chunk_results)
            if not stored_any:
                metrics["summary_slices_failed"] += 1
                log.warning("Slice summarization produced no phases (start=%d end=%d) - watermark NOT advanced", start_idx, end_idx)
                return

            phase_rows = await pool.fetch("SELECT phase_number, summary FROM proxy.phase_summaries WHERE task_id=$1 ORDER BY phase_number", task_uuid)
            phase_history = ""
            for pr in phase_rows:
                phase_history += "Phase " + str(pr["phase_number"]) + ": " + pr["summary"] + "\n"
            if len(phase_history) > 6000:
                phase_history = phase_history[-6000:]
            prior_summary = "No prior summary."
            existing = await pool.fetchrow("SELECT summary FROM proxy.session_summaries WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1", task_uuid)
            if existing and existing["summary"]:
                prior_summary = existing["summary"]
            root_user_msg = "Prior root summary: " + prior_summary + "\n\nFull phase history" + ((((" [Session: " + session_name + "]") if session_name else ""))) + ":\n" + phase_history
            root_result = await _call_4b([{"role": "user", "content": root_user_msg}], max_tokens=1500, json_mode=True, system=MISTRAL_SYSTEM_PROMPT, kind="root")
            root_summary = ""
            if isinstance(root_result, dict):
                su = root_result.get("state_update", {})
                if isinstance(su, dict) and su.get("current_state"):
                    root_summary = su["current_state"].strip()
                if not root_summary:
                    actions = root_result.get("memory_actions", [])
                    if actions and isinstance(actions[0], dict) and actions[0].get("content"):
                        root_summary = actions[0]["content"].strip()
            elif isinstance(root_result, str) and root_result.strip():
                root_summary = root_result.strip()
                if root_summary.startswith("{"):
                    try:
                        parsed = json.loads(root_summary)
                        if isinstance(parsed, dict):
                            su = parsed.get("state_update", {})
                            if isinstance(su, dict) and su.get("current_state"):
                                root_summary = su["current_state"].strip()
                    except Exception:
                        pass
            quality_ok = False
            skip_store = False
            if root_summary:
                quality_ok = True
                quality_reason = ""
                if len(root_summary) < 50:
                    quality_ok = False
                    quality_reason = "too short (%d chars)" % len(root_summary)
                elif root_summary == prior_summary.strip():
                    quality_ok = False
                    quality_reason = "identical to prior summary"
                elif not any(c.isalpha() for c in root_summary):
                    quality_ok = False
                    quality_reason = "no alphabetic content"
                if not quality_ok and quality_reason == "identical to prior summary" and len(slice_msgs) <= 4:
                    quality_ok = True
                    skip_store = True
                    log.info("Root summary identical, tiny slice (%d msgs) - advancing watermark without storing", len(slice_msgs))
                if quality_ok and not skip_store:
                    root_capped = root_summary[:6000]
                    await pool.execute("INSERT INTO proxy.session_summaries (task_id, session_key, summary, trimmed_msg_count, trimmed_tokens) VALUES ($1,$2,$3,$4,$5)",
                                      task_uuid, session_key, root_capped, len(slice_msgs), count_tokens(phase_history))
                    _trim_summary_last[task_uuid] = time.time()
                    log.info("Root summary stored: %d phases total, summary=%d chars", len(phase_rows), len(root_capped))
                else:
                    log.info("Root summary discarded (quality): %s", quality_reason)

            if quality_ok and end_idx > ws.get("summarized_through", 0):
                ws["summarized_through"] = end_idx
                await _window_persist(session_key, ws)
                log.info("Watermark advanced: session=%s summarized_through=%d", session_key, end_idx)
            elif not quality_ok:
                log.warning("Root summary not stored; watermark NOT advanced (slice will be retried): session=%s start=%d end=%d",
                            session_key, start_idx, end_idx)
        except Exception as e:
            metrics["summary_slices_failed"] += 1
            log.warning("Slice summarization failed (watermark NOT advanced, will retry): %s", e)
        finally:
            # BUG 2: always release the in-flight flag on EVERY exit path
            # (early returns, no-phase, exception) so future triggers are not
            # blocked. Previously early returns left in_flight=True forever.
            ws["in_flight"] = False



def _compact_context(frozen_summary: str, working_memory: str, recent_messages: list) -> str:
    """Build a compact context string from frozen summary, working memory, and recent messages."""
    fs = frozen_summary
    fs = fs[:6000]
    parts = []
    if fs:
        parts.append("[FROZEN SUMMARY]" + chr(10) + fs)
    if working_memory:
        parts.append("[WORKING MEMORY]" + chr(10) + working_memory)
    if recent_messages:
        parts.append("[RECENT MESSAGES]" + chr(10) + json.dumps(recent_messages, ensure_ascii=False))
    return chr(10) + chr(10) + chr(10) + chr(10).join(parts)

async def _fetch_session_summary(task_uuid, budget=1500, session_key: str = ""):
    """Fetch the most recent session summary for a task.

    Falls back to matching by session_key prefix (x_sid) if task_uuid has no summary,
    ensuring summaries are found even when task resolution changes.
    """
    if not pool:
        return ""
    try:
        row = await pool.fetchrow("SELECT summary FROM proxy.session_summaries WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1", task_uuid)
        if not row and session_key:
            # Fallback: match by session_key prefix (the x_sid part)
            x_sid = session_key.split(":")[0] if ":" in session_key else session_key
            row = await pool.fetchrow(
                "SELECT ss.summary FROM proxy.session_summaries ss "
                "JOIN proxy.tasks t ON t.id = ss.task_id "
                "WHERE t.session_id = $1 ORDER BY ss.created_at DESC LIMIT 1",
                x_sid
            )
        if row and row["summary"]:
            s = row["summary"].strip()
            t = count_tokens(s)
            if t <= budget:
                return s
            return s[:budget * 4]
    except Exception as e:
        log.warning("Session summary fetch failed: %s", e)
    return ""


# --- Constants ---
VLLM_URL = os.environ.get("CTXGATE_VLLM_URL", "http://127.0.0.1:29000/v1")
VLLM_MODEL = os.environ.get("CTXGATE_VLLM_MODEL", "Qwen3.8-27B")
MAX_CONTEXT = _env_int("CTXGATE_MAX_CONTEXT", 84000)
MAX_INPUT = _env_int("CTXGATE_MAX_INPUT", 58000)
# --- Rolling window (sticky cut) config ---
TRIM_TARGET_TOKENS = _env_int("CTXGATE_TRIM_TARGET_TOKENS", 0)          # absolute; wins if > 0
TRIM_TARGET_FRACTION = _env_float("CTXGATE_TRIM_TARGET_FRACTION", 0.70)
TRIM_TARGET_FLOOR = _env_int("CTXGATE_TRIM_TARGET_FLOOR", 20000)
SUMMARY_BACKFILL_CHARS = _env_int("CTXGATE_SUMMARY_BACKFILL_CHARS", 96000)
KNOWLEDGE_EXTRACT_MIN_CHARS = _env_int("CTXGATE_KNOWLEDGE_EXTRACT_MIN_CHARS", 40)
SUMMARY_CHUNK_CHARS = _env_int("CTXGATE_SUMMARY_CHUNK_CHARS", 24000)
SUMMARY_MAX_CHUNKS = _env_int("CTXGATE_SUMMARY_MAX_CHUNKS", 4)
SUMMARY_TOOL_CAP_CHARS = _env_int("CTXGATE_SUMMARY_TOOL_CAP_CHARS", 1000)
WINDOW_TTL_DAYS = _env_int("CTXGATE_WINDOW_TTL_DAYS", 7)
SSE_HEARTBEAT_INTERVAL = _env_int("CTXGATE_SSE_HEARTBEAT_INTERVAL", 10)
STABLE_ELIDE = os.environ.get("CTXGATE_STABLE_ELIDE", "1") != "0"  # stable tool-body elision (prefix-cache friendly)
MAX_OUTPUT = _env_int("CTXGATE_MAX_OUTPUT", 22500)
SAFETY_MARGIN = _env_int("CTXGATE_SAFETY_MARGIN", 3500)
MIN_OUTPUT = _env_int("CTXGATE_MIN_OUTPUT", 16000)  # hard floor for the output budget
PINNED_USER_MAX_CHARS = _env_int("CTXGATE_PINNED_USER_MAX_CHARS", 16000)  # cap for pinned user copy
PINNED_USER_FAR_CHARS = _env_int("CTXGATE_PINNED_USER_FAR_CHARS", 2000)   # tighter cap when user msg is far before cut
PINNED_USER_FAR_THRESHOLD = _env_int("CTXGATE_PINNED_USER_FAR_THRESHOLD", 8)  # distance in messages to trigger far cap

WALL_CLOCK_MAX = _env_int("CTXGATE_WALL_CLOCK_MAX", 1800)  # 30 min - large contexts need more time
MAX_CONTINUATIONS = int(os.environ.get("CTXGATE_MAX_CONTINUATIONS", 5))
WORKER_BACKPRESSURE = _env_int("CTXGATE_WORKER_BACKPRESSURE", 50)
CTXGATE_MAX_TOTAL_OUTPUT = _env_int("CTXGATE_MAX_TOTAL_OUTPUT", 50000)  # hard cap for the whole logical response incl. all continuations
CTXGATE_MIN_CONTINUATION_OUTPUT = _env_int("CTXGATE_MIN_CONTINUATION_OUTPUT", 1024)  # small floor for later continuation requests
SESSION_TTL_HOURS = _env_int("CTXGATE_SESSION_TTL_HOURS", 12)
SESSION_LAST_ACTIVE: dict[str, float] = {}
WORKER_PENDING_CACHE: dict = {"value": 0, "ts": 0.0}
WORKER_PENDING_TTL = 5.0
MEMORY_TTL_DAYS = _env_int("CTXGATE_MEMORY_TTL_DAYS", 90)
DB_DSN = os.environ.get("CTXGATE_DB_DSN") or os.environ.get("CTXPROXY_DB_DSN") or "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy"
PROXY_PORT = _env_int("CTXGATE_PROXY_PORT", 9201)
API_KEY = os.environ.get("CTXGATE_API_KEY", "")
MAX_BODY_BYTES = _env_int("CTXGATE_MAX_BODY_BYTES", 20 * 1024 * 1024)
QWEN_TOKENIZER_PATH = os.environ.get("CTXGATE_QWEN_TOKENIZER") or os.path.join(
    os.path.expanduser("~"), "models", "Swift-1.5-Qwen3.8-27b-W4A16-AutoRound", "tokenizer.json")
vllm_alive = False  # Updated by _vllm_health_loop

# --- Repetition-loop guard (streaming path) ---
LOOP_REPEATS = _env_int("CTXGATE_LOOP_REPEATS", 4)
LOOP_SENTENCE_REPEATS = _env_int("CTXGATE_LOOP_SENTENCE_REPEATS", 6)
LOOP_TAIL = 4000
LOOP_CHECK_EVERY = 256
MAX_REASONING_TOKENS = _env_int("CTXGATE_MAX_REASONING_TOKENS", 12000)
REPAIR_DANGLING_TOOLCALLS = _env_int("CTXGATE_REPAIR_DANGLING_TOOLCALLS", 1)
LOOP_RETRIES = _env_int("CTXGATE_LOOP_RETRIES", 1)
RETRY_TEMPERATURE = _env_float("CTXGATE_RETRY_TEMPERATURE", 0.7)
RETRY_TOP_P = _env_float("CTXGATE_RETRY_TOP_P", 0.8)
RETRY_PRESENCE_PENALTY = _env_float("CTXGATE_RETRY_PRESENCE_PENALTY", 1.5)
MIN_TEMPERATURE = _env_float("CTXGATE_MIN_TEMPERATURE", 0.3)
PRESENCE_PENALTY = os.environ.get("CTXGATE_PRESENCE_PENALTY", "")
REPETITION_DETECTION = os.environ.get("CTXGATE_REPETITION_DETECTION", "")
THINKING_TOKEN_BUDGET = os.environ.get("CTXGATE_THINKING_TOKEN_BUDGET", "")


# --- Global state (per-session where applicable) ---
pool: Optional[asyncpg.Pool] = None
enc: Optional[Any] = None
sqlite_conn: Optional[aiosqlite.Connection] = None
sqlite_lock = asyncio.Lock()  # serializes access to the shared aiosqlite conn (not safe for concurrent execute)

# Per-session state, keyed by session_key = "{x_session_id}:{content_fp[:8]}"
session_fingerprints: dict[str, str] = {}
# Step 2: per-session anchor of the newest user message already extracted, so we
# extract at most once per user turn. Value = _msg_anchor(newest user msg).
_last_extract_anchor: dict[str, str] = {}
session_seeds: dict[str, list] = {}
session_compactions: dict[str, dict] = {}  # session_key -> frozen [msg0, msg1, msg2] (immutable seed)
session_tokens: dict[str, dict] = {}  # {in, out, reqs, max_ctx}
recent_calls = deque(maxlen=200)
RECENT_CALLS_MAX = 200

metrics = {
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
    "prefix_invalidations": 0,
    "toolcall_strips": 0,
    "tokens_in_total": 0,
    "tokens_out_total": 0,
    "max_context_seen": 0,
    "cached_tokens_total": 0,
    "prompt_tokens_total": 0,
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
    # --- Output integrity metrics ---
    "output_total_budget_exhausted": 0,
    "output_truncated_total": 0,
    "output_truncated_by_reason": {},
    "output_continuations_total": 0,
    "output_max_tokens_seen": 0,
    "tool_call_truncated": 0,
    "tool_call_suppressed": 0,
    "tool_call_complete": 0,
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
}

_background_tasks: set = set()


def _spawn(coro):
    """Create a task and keep a strong reference so GC cannot destroy it while pending."""
    t = asyncio.ensure_future(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)

# --- Injection / utilization metrics (persisted to JSON, survives restart) ---
INJECTION_METRICS_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "injection_metrics.json"))

def _default_injection_metrics() -> dict:
    return {
        "task_memory_injections": 0,
        "task_memory_tokens": 0,
        "knowledge_injections": 0,
        "knowledge_tokens": 0,
        "working_memory_injections": 0,
        "working_memory_tokens": 0,
        "total_requests": 0,
        "sessions": {},
        "last_injected_task_memory": "",
        "last_injected_knowledge": "",
        "events": [],
    }

def _load_injection_metrics() -> dict:
    try:
        with open(INJECTION_METRICS_PATH) as f:
            data = json.load(f)
        base = _default_injection_metrics()
        if isinstance(data, dict):
            base.update(data)
        return base
    except Exception:
        return _default_injection_metrics()

_last_metrics_save = 0.0

def _save_injection_metrics() -> None:
    global _last_metrics_save
    if time.monotonic() - _last_metrics_save < 5.0: return
    _last_metrics_save = time.monotonic()
    try:
        with open(INJECTION_METRICS_PATH, "w") as f:
            json.dump(injection_metrics, f)
    except Exception as e:
        log.warning("injection metrics save failed: %s", e)

def _pct_str(n: int, d: int) -> str:
    return f"{(100 * n / d):.1f}%" if d else "0.0%"

def _record_injection(session_id: str, tm: str, kn: str) -> None:
    """Record memory/knowledge injection utilization. Lightweight, no DB round-trips."""
    global injection_metrics
    try:
        injection_metrics["total_requests"] += 1
        events = []
        if tm:
            t = count_tokens(tm)
            injection_metrics["task_memory_injections"] += 1
            injection_metrics["task_memory_tokens"] += t
            injection_metrics["last_injected_task_memory"] = tm[:2000]
            events.append({"ts": time.time(), "session": session_id, "type": "task_memory", "tokens": t})
        if kn:
            t = count_tokens(kn)
            injection_metrics["knowledge_injections"] += 1
            injection_metrics["knowledge_tokens"] += t
            injection_metrics["last_injected_knowledge"] = kn[:2000]
            events.append({"ts": time.time(), "session": session_id, "type": "knowledge", "tokens": t})
        # Working memory is a subset of task memory; count it when the WM line is present.
        wm_line = ""
        if tm and "WORKING MEMORY:" in tm:
            for ln in tm.split("\n"):
                if ln.startswith("WORKING MEMORY:"):
                    wm_line = ln
                    break
        if wm_line:
            injection_metrics["working_memory_injections"] += 1
            injection_metrics["working_memory_tokens"] += count_tokens(wm_line)
        s = injection_metrics["sessions"].setdefault(session_id, {"task_mem": 0, "knowledge": 0, "wm": 0, "requests": 0})
        s["requests"] += 1
        if tm:
            s["task_mem"] += 1
        if kn:
            s["knowledge"] += 1
        if wm_line:
            s["wm"] += 1
        for e in events:
            injection_metrics["events"].append(e)
        if len(injection_metrics["events"]) > 50:
            injection_metrics["events"] = injection_metrics["events"][-50:]
        _save_injection_metrics()
    except Exception as e:
        log.warning("injection metrics record failed: %s", e)

injection_metrics = _load_injection_metrics()

def _human_time(seconds: float) -> str:
    """Convert seconds to human readable format."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        m = int(seconds // 60)
        s = seconds % 60
        return f"{m}m {s:.0f}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"

def validate_config() -> None:
    """Fail-fast config validation. Exits the process if any problem is found."""
    import urllib.parse
    problems: list[str] = []

    # 1. DB_DSN must parse and have a hostname
    try:
        u = urllib.parse.urlparse(DB_DSN)
        if not u.hostname:
            problems.append(f"DB_DSN has no hostname: {DB_DSN!r}")
    except Exception as e:
        problems.append(f"DB_DSN unparseable: {e}")

    # 2. VLLM_URL must be a valid http(s) URL
    try:
        u2 = urllib.parse.urlparse(VLLM_URL)
        if u2.scheme not in ("http", "https") or not u2.hostname:
            problems.append(f"VLLM_URL is not a valid http(s) URL: {VLLM_URL!r}")
    except Exception as e:
        problems.append(f"VLLM_URL unparseable: {e}")

    # 3. MAX_INPUT + SAFETY_MARGIN <= MAX_CONTEXT
    if MAX_INPUT + SAFETY_MARGIN > MAX_CONTEXT:
        problems.append(f"MAX_INPUT({MAX_INPUT}) + SAFETY_MARGIN({SAFETY_MARGIN}) > MAX_CONTEXT({MAX_CONTEXT})")

    # 4. MAX_OUTPUT >= 1
    if MAX_OUTPUT < 1:
        problems.append(f"MAX_OUTPUT must be >= 1, got {MAX_OUTPUT}")

    # 5. Tokenizer path must exist if set
    if QWEN_TOKENIZER_PATH and not os.path.exists(QWEN_TOKENIZER_PATH):
        problems.append(f"Tokenizer path does not exist: {QWEN_TOKENIZER_PATH!r}")

    for p in problems:
        log.error("config: %s", p)

    if problems:
        log.error("config validation FAILED with %d problem(s); refusing to start", len(problems))
        sys.exit(1)

    log.info("config validation OK (dsn=%s vllm=%s max_ctx=%d max_in=%d max_out=%d margin=%d)",
             DB_DSN, VLLM_URL, MAX_CONTEXT, MAX_INPUT, MAX_OUTPUT, SAFETY_MARGIN)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, enc
    validate_config()
    # Retry DB connection (service starts independently, waits for PG)
    for _attempt in range(60):
        try:
            pool = await asyncpg.create_pool(
                DB_DSN, min_size=2, max_size=10,
                command_timeout=10, timeout=10,
                max_inactive_connection_lifetime=300,
            )
            break
        except Exception as _e:
            log.warning("DB not ready (attempt %d/60): %s - retrying in 2s", _attempt + 1, _e)
            await asyncio.sleep(2)
            if _attempt == 59:
                raise RuntimeError("Cannot connect to PostgreSQL after 120s") from _e
    log.info("DB pool connected")
    tok_name = "cl100k_base (fallback)"
    try:
        enc = tokenizers.Tokenizer.from_file(QWEN_TOKENIZER_PATH)
        tok_name = "Qwen tokenizer.json"
        try:
            vocab = enc.get_vocab_size()
        except Exception:
            vocab = -1
        log.info("ctxgate-proxy started: tokenizer=%s vocab=%d, vllm=%s model=%s, api_key=%s",
                 tok_name, vocab, VLLM_URL, VLLM_MODEL, "set" if API_KEY else "off")
    except Exception as e:
        enc = tiktoken.get_encoding("cl100k_base")
        log.warning("ctxgate-proxy: Qwen tokenizer load FAILED (%s), falling back to cl100k_base", e)

    # Start memory worker background loop
    worker_task = asyncio.create_task(_memory_worker_loop())
    health_task = asyncio.create_task(_vllm_health_loop())
    global _hygiene_task
    hygiene_task = asyncio.create_task(_fd_hygiene_loop())
    _hygiene_task = hygiene_task
    global _vllm_client
    _vllm_timeout = httpx.Timeout(
        _env_int("CTXGATE_VLLM_READ_TIMEOUT", 300),
        connect=_env_int("CTXGATE_VLLM_CONNECT_TIMEOUT", 10),
        write=_env_int("CTXGATE_VLLM_WRITE_TIMEOUT", 120),
        pool=_env_int("CTXGATE_VLLM_POOL_TIMEOUT", 30),
    )
    _vllm_client = httpx.AsyncClient(timeout=_vllm_timeout, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
    log.info("shared vLLM httpx client created (timeout=%s)", _vllm_timeout)
    # Start Mistral priority queue + shared client + consumer
    global _lm_queue, _lm_client
    _lm_queue = asyncio.PriorityQueue()
    _lm_timeout = httpx.Timeout(MISTRAL_TIMEOUT, connect=10)
    _lm_client = httpx.AsyncClient(timeout=_lm_timeout, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"} if MISTRAL_API_KEY else {})
    log.info("shared Mistral httpx client created (timeout=%ds, connect=10s, api_key=%s)", MISTRAL_TIMEOUT, "set" if MISTRAL_API_KEY else "MISSING")
    LM_WORKERS = int(os.environ.get("CTXGATE_LM_WORKERS", "10"))
    lm_consumer_tasks = [asyncio.create_task(_lm_consumer(i)) for i in range(LM_WORKERS)]
    log.info("Mistral %d independent workers started (rate limiter: %.1f RPS, %d TPM, model=%s)",
             LM_WORKERS, MistralRateLimiter.RPS_LIMIT, MistralRateLimiter.TPM_LIMIT, MISTRAL_MODEL)
    _sd_notify("READY=1")
    watchdog_task = asyncio.create_task(_watchdog_loop())
    log.info("systemd watchdog loop started (10s heartbeat, 60s timeout in unit)")
    yield
    # Cancel background tasks FIRST so they stop using the pool
    worker_task.cancel()
    health_task.cancel()
    hygiene_task.cancel()
    watchdog_task.cancel()
    try:
        await worker_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await health_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await hygiene_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await watchdog_task
    except (asyncio.CancelledError, Exception):
        pass
    # Shutdown Mistral queue + client
    for t in lm_consumer_tasks:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    if _lm_client is not None:
        await _lm_client.aclose()
        _lm_client = None
        _lm_queue = None
    if _vllm_client is not None:
        await _vllm_client.aclose()
        _vllm_client = None
    if sqlite_conn:
        await sqlite_conn.close()
    if pool:
        await pool.close()
    log.info("ctxgate-proxy shutdown complete (graceful: pool drained)")

app = FastAPI(title="local-llm-ctxgate-proxy", version="1.0.0", lifespan=lifespan)

# --- Token counting ---
def count_tokens(text: str) -> int:
    if not enc:
        return len(text) // 4
    try:
        return len(enc.encode(text).ids)
    except AttributeError:
        return len(enc.encode(text))

def count_message_tokens(msg: dict) -> int:
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    tokens = count_tokens(f"\n{msg.get('role', '')}") + count_tokens(content)
    tc = msg.get("tool_calls")
    if tc:
        tokens += count_tokens(json.dumps(tc))
    if msg.get("tool_call_id"):
        tokens += count_tokens(msg["tool_call_id"])
    return tokens

def count_messages_tokens(messages: list) -> int:
    if not messages:
        return 0
    parts = []
    for m in messages:
        r = m.get("role", "")
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        parts.append("\n" + r + " " + c)
        tc = m.get("tool_calls")
        if tc: parts.append(" " + json.dumps(tc))
        if m.get("tool_call_id"): parts.append(" " + m["tool_call_id"])
    full = " ".join(parts)
    if not enc:
        return len(full) // 4 + len(messages) * 12
    try:
        return len(enc.encode(full).ids) + len(messages) * 12
    except AttributeError:
        return len(enc.encode(full)) + len(messages) * 12
def _prefix_diag(session_key: str, messages: list) -> None:
    """P8: compare per-message sha1 of the FINAL upstream messages against the
    previous request. Logs how many leading messages are byte-identical (the
    prefix that vLLM can KV-cache-reuse) and how many are new."""
    if not session_key:
        return
    def _mh(m):
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        tc = m.get("tool_calls")
        tc_s = json.dumps(tc, sort_keys=True) if tc else ""
        return hashlib.sha1((str(m.get("role", "")) + "|" + str(c) + "|" + tc_s).encode()).hexdigest()
    cur = [_mh(m) for m in messages]
    prev = session_prefix_hashes.get(session_key)
    if prev is not None:
        stable = 0
        for i in range(min(len(prev), len(cur))):
            if prev[i] == cur[i]:
                stable += 1
            else:
                break
        new_msgs = len(cur) - stable
        log.info("PREFIX session=%s stable_msgs=%d of %d new_msgs=%d", session_key, stable, len(cur), new_msgs)
    session_prefix_hashes[session_key] = cur

# --- Session key (D11 per-session) ---
def _prefix_raw(messages: list) -> str:
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

def make_session_key(x_session_id: str, messages: list) -> str:
    fp = hashlib.sha256(_prefix_raw(messages).encode()).hexdigest()[:8]
    return f"{x_session_id}:{fp}"

# --- Prefix fingerprint (per-session) ---
def compute_prefix_fingerprint(messages: list) -> str:
    """Deterministic fingerprint of the message prefix for session tracking."""
    return hashlib.sha256(_prefix_raw(messages).encode()).hexdigest()[:16]
def check_prefix(session_key: str, messages: list) -> None:
    """Check if the message prefix changed since last seen. Increment invalidation counter on change."""
    fp = compute_prefix_fingerprint(messages)
    prev = session_fingerprints.get(session_key)
    if prev is not None and prev != fp:
        metrics["prefix_invalidations"] += 1
        log.info("Prefix invalidation session=%s (fp %s -> %s)", session_key, prev[:8], fp[:8])
    session_fingerprints[session_key] = fp





# --- D10: Reasoning stripping ---

# --- D9: Malformed tool-call sanitization ---

# Phase 2: Repair dangling tool calls in the seed
_DANGLING_PLACEHOLDER = "[earlier tool call archived]"

def _repair_dangling_tool_calls(msgs: list) -> list:
    """Return a copy of msgs with dangling tool_calls removed.

    A tool_call is "dangling" if its tool_call_id has no matching
    role="tool" message later in the list. This happens when the seed
    (first 3 messages) includes an assistant message with a tool_call
    whose result was cut by the window.

    - Pure function: never mutates the input.
    - Idempotent: running it twice gives the same result.
    - Byte-identical: same input always produces same output.
    - Only affects assistant messages with dangling tool_calls.
    - If all tool_calls are dangling, the key is removed.
    - If content is empty after removal, a placeholder is inserted.

    Env: CTXGATE_REPAIR_DANGLING_TOOLCALLS (default 1).
    """
    if not REPAIR_DANGLING_TOOLCALLS:
        return msgs
    if not msgs:
        return msgs

    # Collect all tool_call_ids that HAVE a matching tool result
    resolved_ids = set()
    for m in msgs:
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            if tcid:
                resolved_ids.add(tcid)

    # Check if any repair is needed
    needs_repair = False
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id", "")
                if tcid and tcid not in resolved_ids:
                    needs_repair = True
                    break
            if needs_repair:
                break

    if not needs_repair:
        return msgs

    # Build the repaired copy
    result = []
    for m in msgs:
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            result.append(m)
            continue

        # Filter out dangling tool_calls
        kept_calls = []
        for tc in m["tool_calls"]:
            tcid = tc.get("id", "")
            if tcid and tcid not in resolved_ids:
                continue  # dangling - drop it
            kept_calls.append(tc)

        if len(kept_calls) == len(m["tool_calls"]):
            # No dangling calls - keep as-is
            result.append(m)
            continue

        # Create a copy with dangling calls removed
        new_m = dict(m)
        if kept_calls:
            new_m["tool_calls"] = kept_calls
        else:
            new_m.pop("tool_calls", None)
            # If content is empty, add a deterministic placeholder
            content = new_m.get("content", "")
            if not content or (isinstance(content, str) and not content.strip()):
                new_m["content"] = _DANGLING_PLACEHOLDER
        result.append(new_m)

    return result

def _detect_loop(text: str) -> int:
    """Detect a repetition loop in the recent tail of generated text.

    Returns the loop period in chars (>0) when a loop is found, else 0.
    Only the last LOOP_TAIL chars are examined (never the full text).
    Two independent tests:
      1) periodic suffix  - the last 40 chars recur at a period p>=20 and the
         last 4*p chars equal the last p chars repeated LOOP_REPEATS times.
      2) sentence repeat  - a normalised sentence of >=30 chars seen
         >= LOOP_SENTENCE_REPEATS times in the last 4000 chars.
    """
    if not text or len(text) < 40:
        return 0
    tail = text[-LOOP_TAIL:]
    probe = tail[-40:]
    prev = tail.rfind(probe, 0, len(tail) - 40)
    p = (len(tail) - 40) - prev if prev >= 0 else 0
    if p >= 20 and len(tail) >= 4 * p and tail[-4 * p:] == tail[-p:] * LOOP_REPEATS:
        # F14: reject multi-line periods (tables, code blocks, test output)
        unit = tail[-p:]
        if unit.count("\n") <= 1:
            return p
    sents = [re.sub(r"\s+", " ", x.strip().lower()) for x in re.split(r"(?<=[.!?])\s+|\n+", tail[-4000:])]
    counts = {}
    for x in sents:
        if len(x) >= 30:
            counts[x] = counts.get(x, 0) + 1
            if counts[x] >= LOOP_SENTENCE_REPEATS:
                return len(x)
    # F14: consecutive word repetition (catches "hello hello hello ..." where
    # the period is < 20 chars and the sentence is < 30 chars).
    # Only triggers when the SAME word appears 10+ times CONSECUTIVELY.
    words = re.findall(r"[a-zA-Z\u00C0-\u024F]{3,}", tail[-4000:].lower())
    if len(words) >= 10:
        run_len = 1
        for i in range(1, len(words)):
            if words[i] == words[i - 1]:
                run_len += 1
                if run_len >= 10:
                    return len(words[i])
            else:
                run_len = 1
    return 0


def _classify_truncation(finish_reason: str, content: str, reasoning_content: str, tool_calls: list) -> str:
    reasoning_tok = count_tokens(reasoning_content or "")
    content_len = len(content or "")
    if finish_reason == "length":
        if reasoning_tok >= 12000 and content_len < 50:
            return "reasoning_overflow"
        return "content_truncation"
    if finish_reason == "tool_calls":
        for tc in (tool_calls or []):
            try:
                args = tc.get("function", {}).get("arguments", "")
                if args:
                    json.loads(args)
            except (json.JSONDecodeError, ValueError):
                return "tool_call_truncation"
        return "none"
    return "none"

def sanitize_tool_calls(message: dict) -> tuple:
    tc = message.get("tool_calls")
    if not tc:
        return message, False
    valid = []
    all_valid = True
    for t in tc:
        if not isinstance(t, dict):
            all_valid = False
            break
        if "id" not in t or "type" not in t or "function" not in t:
            all_valid = False
            break
        fn = t.get("function", {})
        if "name" not in fn or "arguments" not in fn:
            all_valid = False
            break
        args_str = fn["arguments"]
        if isinstance(args_str, str):
            try:
                json.loads(args_str)
            except (json.JSONDecodeError, ValueError):
                all_valid = False
                break
        elif not isinstance(args_str, dict):
            all_valid = False
            break
        valid.append(t)
    if all_valid:
        return message, False
    log.warning("D9: stripped malformed tool_calls from assistant message id=%s", message.get("id", "?"))
    cleaned = {k: v for k, v in message.items() if k != "tool_calls"}
    if not cleaned.get("content"):
        cleaned["content"] = ""
    return cleaned, True

# --- Context assembler ---
def strip_reasoning(messages: list) -> list:
    """Canonical: remove hidden reasoning fields from assistant messages.

    Strips both the legacy 'reasoning' key and 'reasoning_content'. Assistant
    messages carrying either are returned without them; all other messages
    pass through unchanged. Every returned message is a shallow copy so
    callers may mutate freely. Single source of truth for reasoning removal -
    used by _prep_messages and exposed for tests / backward compatibility.
    """
    out = []
    for m in messages:
        if m.get("role") == "assistant" and ("reasoning" in m or "reasoning_content" in m):
            m = {k: v for k, v in m.items() if k not in ("reasoning", "reasoning_content")}
        out.append(dict(m))
    return out


def _prep_messages(raw: list) -> list:
    out = []
    for m in strip_reasoning(raw):
        if m.get("content") is None:
            m["content"] = ""
        tc = m.get("tool_calls")
        if tc:
            valid = True
            for t in tc:
                if not isinstance(t, dict) or "id" not in t or "type" not in t or "function" not in t:
                    valid = False; break
                fn = t.get("function", {})
                if "name" not in fn or "arguments" not in fn:
                    valid = False; break
                a = fn["arguments"]
                if isinstance(a, str):
                    try:
                        json.loads(a)
                    except (json.JSONDecodeError, ValueError):
                        valid = False; break
                elif not isinstance(a, dict):
                    valid = False; break
            if not valid:
                m = {k: v for k, v in m.items() if k != "tool_calls"}
                if not m.get("content"): m["content"] = ""
                metrics["toolcall_strips"] += 1
        out.append(m)
    if not any(m.get("role") == "user" for m in out):
        out.append({"role": "user", "content": "Please proceed."})
    return out


# --- Rolling window (sticky cut) helpers ---
def _trim_target() -> int:
    """Low-watermark target for the kept window. Absolute wins if > 0, else
    fraction of MAX_INPUT, clamped to [floor_eff, MAX_INPUT]."""
    floor_eff = min(TRIM_TARGET_FLOOR, MAX_INPUT // 2)
    if TRIM_TARGET_TOKENS > 0:
        t = TRIM_TARGET_TOKENS
    else:
        t = int(MAX_INPUT * TRIM_TARGET_FRACTION)
    return max(floor_eff, min(t, MAX_INPUT))

def _norm_content(c) -> str:
    """Normalize a message's content for identity comparison (F2).

    Raw and prepped messages must give IDENTICAL anchors: None -> "" and a
    list of parts -> its joined text parts. _prep_messages does exactly this,
    so a raw (content=None / list) message now matches its prepped twin."""
    if c is None:
        return ""
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c)

def _msg_anchor(m: dict) -> str:
    """Stable identity of a message: role + normalized content[:300] + tool_call_id."""
    c = _norm_content(m.get("content"))
    return hashlib.sha1((str(m.get("role", "")) + "|" + c[:300] + "|" + str(m.get("tool_call_id", ""))).encode()).hexdigest()

def _seed_sig(messages: list) -> str:
    h = hashlib.sha1()
    for m in messages[:3]:
        c = _norm_content(m.get("content"))
        h.update((str(m.get("role", "")) + "|" + c).encode())
    return h.hexdigest()

def _window_lock(session_key: str) -> asyncio.Lock:
    lk = _window_locks.get(session_key)
    if lk is None:
        lk = asyncio.Lock()
        _window_locks[session_key] = lk
    return lk

def _new_window_state(cut: int, rest: list, seed: list, summarized_through: int) -> dict:
    ca = _msg_anchor(rest[cut]) if 0 <= cut < len(rest) else ""
    cp = _msg_anchor(rest[cut - 1]) if 0 <= cut - 1 < len(rest) else ""
    return {
        "cut": cut,
        "cut_anchor": ca,
        "cut_prev_anchor": cp,
        "seed_sig": _seed_sig(seed),
        "dropped_total": cut,
        "summarized_through": summarized_through,
        "pending_cut": cut,
        "in_flight": False,
        "elide_idx": None,
    }

def _window_valid(ws: dict, seed: list, rest: list) -> bool:
    """O(1) validation: seed signature + two anchors at the stored cut."""
    if not ws:
        return False
    cut = ws.get("cut", 0)
    if cut < 0 or cut > len(rest):
        return False
    if ws.get("seed_sig") != _seed_sig(seed):
        return False
    ca = _msg_anchor(rest[cut]) if cut < len(rest) else ""
    if ws.get("cut_anchor", "") != ca:
        return False
    cp = _msg_anchor(rest[cut - 1]) if cut - 1 >= 0 else ""
    if ws.get("cut_prev_anchor", "") != cp:
        return False
    return True

STUB_TEXT = "[COMPACTED HISTORY] earlier turns archived; see TASK STATE"

def _make_pinned_copy(m: dict, distance: int = 0) -> dict:
    """Deterministic pinned copy of a user message. Keeps the first 300 chars
    verbatim (so _msg_anchor, which hashes content[:300], matches the raw message
    and the 'newest user missing' check passes) and caps the total with a
    head + middle-marker + tail truncation. Distance-aware: when the message is
    far before the cut (distance > PINNED_USER_FAR_THRESHOLD), use a tighter cap.

    Adds internal proxy-state markers (ctxgate_pinned, ctxgate_anchor) that are
    NOT part of the model-visible content text."""
    c = _norm_content(m.get("content"))
    CAP = PINNED_USER_FAR_CHARS if distance > PINNED_USER_FAR_THRESHOLD else PINNED_USER_MAX_CHARS
    if len(c) <= CAP:
        text = c
    else:
        # Head + middle-marker + tail: both ends survive so constraints are not lost
        head_len = 300  # anchor-compatible
        tail_len = min(CAP - head_len - 50, len(c) - head_len)
        if tail_len < 0:
            tail_len = 0
        marker = "\n[...middle omitted by ctxgate...]\n"
        text = c[:head_len] + marker + c[len(c) - tail_len:] if tail_len > 0 else c[:head_len] + marker
    # Internal proxy state (NOT in model-visible content)
    anchor = hashlib.sha1(c[:300].encode()).hexdigest()
    return {"role": "user", "content": text, "ctxgate_pinned": True, "ctxgate_anchor": anchor}

def _pinned_user_copy(rest: list, cut: int):
    """Return the pinned copy of the newest user message if it lies before the
    budget cut (the walk would otherwise drop it), else None. Shared by
    _kept_messages and the sticky fast path so the prefix stays byte-stable."""
    lu = _newest_user_idx(rest)
    if lu is None or lu >= cut:
        return None
    return _make_pinned_copy(rest[lu], distance=cut - lu)

def _kept_messages(seed: list, rest: list, cut: int) -> list:
    """Rebuild the kept window: seed + constant stub + [pinned newest-user copy] +
    rest[cut:]. O(kept). The pinned copy is inserted only when the newest user
    message sits before the cut, so the window stays bounded instead of keeping
    the entire post-user tail (the old 'pin by keeping everything' bug)."""
    result = list(seed)
    result.append({"role": "system", "content": STUB_TEXT})
    pinned = _pinned_user_copy(rest, cut)
    if pinned is not None:
        result.append(pinned)
    result.extend(rest[cut:])
    return result

def _newest_user_idx(rest: list):
    """Return the index of the newest user message in rest, or None."""
    for i in range(len(rest) - 1, -1, -1):
        if rest[i].get("role") == "user":
            return i
    return None


def _recut_to(messages: list, max_tokens: int) -> int:
    """Existing budget walk to a boundary-aligned cut targeting max_tokens.

    Boundary rule (F1): after the newest-first budget walk, drop ONLY leading
    'tool' messages (orphans whose assistant lies before the cut). A leading
    assistant is a valid window start because its tool results follow inside the
    kept suffix, so we never drain assistant messages. A post-check guards
    against window collapse and falls back to the plain budget tail.
    """
    seed = list(messages[:3])
    rest = list(messages[3:])
    stub_tok = count_message_tokens({"role": "system", "content": STUB_TEXT})
    seed_tok = count_messages_tokens(seed)
    total_tok = count_messages_tokens(messages)
    lu = _newest_user_idx(rest)
    pinned_copy_tok = count_message_tokens(_make_pinned_copy(rest[lu])) if lu is not None else 0
    budget = max(500, max_tokens - seed_tok - stub_tok - pinned_copy_tok - 200)
    tail = []
    t = 0
    for m in reversed(rest):
        mt = count_message_tokens(m)
        if t + mt > budget and len(tail) > 5:
            break
        tail.append(m)
        t += mt
    tail.reverse()
    # Drop only leading orphan 'tool' messages (their assistant is before the cut).
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    cut = max(0, len(rest) - len(tail))
    # F1 post-check: guard against window collapse (never keep <0.5*target when the
    # history is large). On failure fall back to the plain budget tail.
    kept_tok = count_messages_tokens(_kept_messages(seed, rest, cut))
    if kept_tok < 0.5 * max_tokens and total_tok > max_tokens and len(rest) > 0:
        log.error("WINDOW COLLAPSE guard: kept_tok=%d target=%d total_tok=%d cut=%d rest=%d - falling back to plain budget tail",
                  kept_tok, max_tokens, total_tok, cut, len(rest))
        metrics["window_collapse_guard"] += 1
        ft = []
        ft_tok = 0
        for m in reversed(rest):
            mt = count_message_tokens(m)
            if ft_tok + mt > budget and len(ft) > 5:
                break
            ft.append(m)
            ft_tok += mt
        ft.reverse()
        while ft and ft[0].get("role") == "tool":
            ft.pop(0)
        cut = max(0, len(rest) - len(ft))
    if lu is not None and cut > lu:
        log.warning("RECUT pin: newest user msg at rest[%d] was past cut=%d - inserting pinned copy (window stays bounded)", lu, cut)
        metrics["recut_user_pinned"] += 1
    return cut

def _recut(messages: list) -> int:
    return _recut_to(messages, _trim_target())

log.info("Budget config: ctx=%d input=%d output=%d margin=%d min_output=%d ceiling=%d trim_target=%d",
         MAX_CONTEXT, MAX_INPUT, MAX_OUTPUT, SAFETY_MARGIN, MIN_OUTPUT,
         min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT), _trim_target())
if MAX_INPUT + MAX_OUTPUT + SAFETY_MARGIN > MAX_CONTEXT:
    log.warning("Budget config invalid: MAX_INPUT(%d) + MAX_OUTPUT(%d) + SAFETY_MARGIN(%d) > MAX_CONTEXT(%d)",
                MAX_INPUT, MAX_OUTPUT, SAFETY_MARGIN, MAX_CONTEXT)
if MIN_OUTPUT > MAX_OUTPUT:
    log.warning("Budget config invalid: MIN_OUTPUT(%d) > MAX_OUTPUT(%d)", MIN_OUTPUT, MAX_OUTPUT)

def _output_budget(input_tokens: int) -> int:
    """Single authoritative output budget: never exceed MAX_OUTPUT, and never go
    negative. The caller enforces the MIN_OUTPUT floor (via _emergency_shrink /
    a 413) before sending a starved request."""
    return min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)

def _protected_indices(work: list) -> set:
    """Indices that _emergency_shrink must never drop: the seed (first 3), the
    stub (first system after seed), the pinned newest-user copy (flagged), and
    the canonical protected tool groups (newest 4 tool results + their assistant
    tool_calls declarations). Uses protected_tool_groups as the single source
    of truth for recent tool body protection."""
    protected = set(range(min(3, len(work))))
    for i in range(3, len(work)):
        if work[i].get("role") == "system":
            protected.add(i)
            break
    for i in range(3, len(work)):
        if work[i].get("ctxgate_pinned"):
            protected.add(i)
            break
    # Canonical: newest 4 tool groups (replaces old "newest 6" ad-hoc logic)
    protected |= protected_tool_groups(work)
    return protected


# --- Canonical helpers (single source of truth, all paths use these) ---

def protected_tool_groups(messages: list) -> set:
    """Return the set of indices that must NEVER be elided/dropped:
    the newest 4 tool-result messages AND the assistant message(s) that
    declare their tool_calls (so the tool-call graph stays valid).

    Walk from the end, count tool messages up to 4, and for each, also
    include the nearest preceding assistant message that has tool_calls
    referencing them. This is the SINGLE definition of "recent tool body".
    """
    protected = set()
    tool_count = 0
    for i in range(len(messages) - 1, -1, -1):
        if tool_count >= 4:
            break
        m = messages[i]
        if m.get("role") == "tool":
            protected.add(i)
            tool_count += 1
            # Find the nearest preceding assistant message with tool_calls
            for j in range(i - 1, -1, -1):
                if messages[j].get("role") == "assistant":
                    if messages[j].get("tool_calls"):
                        protected.add(j)
                    break
    return protected

def ctxgate_meta(truncated: bool, reason: str, continuations_used: int,
                total_output_tokens: int, tool_calls_complete: bool,
                tool_calls_emitted: int, tool_call_truncated: bool = False) -> dict:
    """Build the structured ctxgate metadata dict for SSE final chunks."""
    meta = {
        "ctxgate": {
            "truncated": truncated,
            "reason": reason,
            "continuations_used": continuations_used,
            "total_output_tokens": total_output_tokens,
            "tool_calls_complete": tool_calls_complete,
            "tool_calls_emitted": tool_calls_emitted,
        }
    }
    if tool_call_truncated:
        meta["ctxgate"]["tool_call_truncated"] = True
    return meta

class ToolCallAccumulator:
    """Tracks per-tool-call streaming deltas and validates completeness.

    A tool-call set is EXECUTABLE only when every emitted call is complete
    AND its arguments are valid JSON.
    """
    def __init__(self):
        self._calls = {}  # index -> {id, name, arguments}

    def add_delta(self, tool_calls_piece: list):
        """Merge streaming deltas by index."""
        if not tool_calls_piece:
            return
        for piece in tool_calls_piece:
            if not isinstance(piece, dict):
                continue
            idx = piece.get("index", 0)
            if idx not in self._calls:
                self._calls[idx] = {"id": "", "name": "", "arguments": ""}
            call = self._calls[idx]
            if piece.get("id"):
                call["id"] = piece["id"]
            fn = piece.get("function") or {}
            if fn.get("name"):
                call["name"] += fn["name"]
            if fn.get("arguments"):
                call["arguments"] += fn["arguments"]

    def is_complete(self) -> bool:
        """Every seen call has a non-empty name and its arguments parse as valid JSON."""
        if not self._calls:
            return False
        for call in self._calls.values():
            if not call["name"]:
                return False
            if not call["arguments"]:
                return False
            try:
                json.loads(call["arguments"])
            except (json.JSONDecodeError, ValueError):
                return False
        return True

    def is_valid(self) -> bool:
        """Alias for is_complete (every call is structurally valid)."""
        return self.is_complete()

    def to_tool_calls(self) -> list:
        """The complete OpenAI-format list of tool calls."""
        result = []
        for idx in sorted(self._calls.keys()):
            call = self._calls[idx]
            result.append({
                "id": call["id"] or f"call_{idx}",
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": call["arguments"],
                },
            })
        return result

    def count(self) -> int:
        return len(self._calls)

def verify_context_invariants(context: list, original: list, ceiling: int) -> list:
    """Return a list of violation strings (empty = OK).

    Checks:
    (1) newest live user message is represented
    (2) pinned copy exists when the newest user msg was cut from the window
    (3) newest 4 tool bodies are byte-identical to original
    (4) no orphan tool result
    (5) no assistant tool_calls declaration left without its required tool results
    (6) total tokens <= ceiling
    """
    violations = []

    # (1) & (2) newest user message
    orig_newest_user = None
    for i in range(len(original) - 1, -1, -1):
        if original[i].get("role") == "user":
            orig_newest_user = i
            break
    if orig_newest_user is not None:
        orig_user_content = _norm_content(original[orig_newest_user].get("content"))
        found_in_ctx = False
        pinned_exists = False
        for m in context:
            if m.get("role") == "user":
                if _norm_content(m.get("content"))[:300] == orig_user_content[:300]:
                    found_in_ctx = True
                    break
            if m.get("ctxgate_pinned"):
                pinned_exists = True
        if not found_in_ctx and not pinned_exists:
            violations.append("newest_user_missing: newest user message not represented in context")

    # (3) newest 4 tool bodies byte-identical
    prot = protected_tool_groups(original)
    tool_idx_in_orig = 0
    for i in range(len(original) - 1, -1, -1):
        if tool_idx_in_orig >= 4:
            break
        if original[i].get("role") == "tool" and i in prot:
            orig_content = _norm_content(original[i].get("content"))
            tcid = original[i].get("tool_call_id", "")
            matched = False
            for m in context:
                if m.get("role") == "tool" and m.get("tool_call_id") == tcid:
                    if _norm_content(m.get("content")) != orig_content:
                        violations.append(f"tool_body_modified: tool msg at orig[{i}] (id={tcid}) content differs")
                    matched = True
                    break
            if not matched:
                violations.append(f"tool_body_missing: tool msg at orig[{i}] (id={tcid}) not in context")
            tool_idx_in_orig += 1

    # (4) no orphan tool result
    for i, m in enumerate(context):
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id", "")
            has_parent = False
            for j in range(i - 1, -1, -1):
                if context[j].get("role") == "assistant" and context[j].get("tool_calls"):
                    for tc in context[j]["tool_calls"]:
                        if tc.get("id") == tcid:
                            has_parent = True
                            break
                if has_parent:
                    break
            if not has_parent:
                violations.append(f"orphan_tool: tool msg at ctx[{i}] (id={tcid}) has no matching assistant tool_call")

    # (5) no assistant tool_calls without required results
    for i, m in enumerate(context):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id", "")
                orig_had_result = False
                for om in original:
                    if om.get("role") == "tool" and om.get("tool_call_id") == tcid:
                        orig_had_result = True
                        break
                if orig_had_result:
                    ctx_has_result = False
                    for cm in context[i+1:]:
                        if cm.get("role") == "tool" and cm.get("tool_call_id") == tcid:
                            ctx_has_result = True
                            break
                    if not ctx_has_result:
                        violations.append(f"missing_tool_result: assistant at ctx[{i}] declares tool_call {tcid} but result missing")

    # (6) total tokens <= ceiling
    total = count_messages_tokens(context)
    if total > ceiling:
        violations.append(f"over_ceiling: context is {total} tokens > ceiling {ceiling}")

    return violations

def _elided_tool_content(c: str) -> str:
    """The exact step-(a) elision body used by _emergency_shrink (1000 head +
    marker + 1000 tail). Shared so stable elision and emergency shrink produce
    byte-identical content for the same input."""
    if "chars elided by ctxgate" in c:
        return c
    elided = len(c) - 2000
    return c[:1000] + "\n[...%d chars elided by ctxgate...]\n" % elided + c[-1000:]

def _stable_elide_inplace(msgs: list, start: int, end: int) -> None:
    """Elide in-place every 'tool' message with string content > 2500 chars
    in msgs[start:end]. Content > 2500 becomes head+tail (~2550 chars, back
    below the 2500 threshold) so the operation is IDEMPOTENT and deterministic:
    the same message elides to the same bytes on every call, keeping the sent
    prefix byte-stable across re-cuts. Never touches non-tool messages; the
    caller computes start/end so seed/stub/pinned are never elided."""
    if start < 0:
        start = 0
    end = min(end, len(msgs))
    if start >= end:
        return
    for i in range(start, end):
        m = msgs[i]
        if m.get("role") != "tool":
            continue
        c = m.get("content")
        if isinstance(c, str) and len(c) > 2500:
            m["content"] = _elided_tool_content(c)

def _stable_elide_idx(cut: int, rest: list) -> int:
    """Absolute index into raw 'rest' marking the elidable boundary:
    the index of the 4th-newest tool-result message (or 'cut', whichever is
    larger). Everything in [cut, idx) is elidable; [idx, end) keeps the
    newest 4 tool bodies intact (plus any interleaved assistant/user msgs).
    Uses protected_tool_groups as the canonical definition."""
    prot = protected_tool_groups(rest)
    tool_indices = sorted([i for i in prot if rest[i].get("role") == "tool"])
    if tool_indices:
        idx = tool_indices[0]  # oldest of the newest-4
    else:
        idx = len(rest)
    return max(cut, idx)

class ContextCapacityError(Exception):
    """Raised when protected material alone exceeds the ceiling."""
    def __init__(self, message: str, before: int, ceiling: int):
        super().__init__(message)
        self.before = before
        self.ceiling = ceiling

def _emergency_shrink(kept: list, ceiling: int) -> list:
    """Hard invariant: shrink a built window to <= ceiling tokens without breaking
    the tool-call graph. Used by build_context (both paths) and the 400 re-trim.

    Canonical order:
      1. preserve seed
      2. preserve pinned newest-user copy
      3. preserve newest-4 tool groups (via protected_tool_groups)
      4. elide OLDER tool bodies only (skip protected)
      5. drop oldest complete assistant/user/tool groups
      6. if STILL over because PROTECTED material exceeds ceiling ->
         raise ContextCapacityError (caller turns into 413)

    Returns the (possibly new) list; never mutates the caller list."""
    before = count_messages_tokens(kept)
    if before <= ceiling:
        return kept
    metrics["emergency_shrink_total"] += 1
    work = [dict(m) for m in kept]
    protected = _protected_indices(work)
    def _tok():
        return count_messages_tokens(work)
    # (4) elide large tool bodies, oldest-first, SKIP protected
    for i in range(len(work)):
        if _tok() <= ceiling:
            break
        if i in protected:
            continue
        m = work[i]
        if m.get("role") != "tool":
            continue
        c = m.get("content")
        if not isinstance(c, str) or len(c) <= 2500:
            continue
        work[i]["content"] = _elided_tool_content(c)
    # (5) drop oldest whole groups while still over
    dropped_groups = 0
    while _tok() > ceiling:
        start = None
        for i in range(len(work)):
            if i in protected:
                continue
            if work[i].get("role") in ("assistant", "user"):
                start = i
                break
        if start is None:
            break
        end = start
        if work[start].get("role") == "assistant" and work[start].get("tool_calls"):
            j = start + 1
            while j < len(work) and work[j].get("role") == "tool":
                end = j
                j += 1
        while end > start and end in protected:
            end -= 1
        del work[start:end + 1]
        dropped_groups += 1
    metrics["emergency_shrink_groups_dropped"] += dropped_groups
    # (6) if STILL over: protected material alone exceeds ceiling
    if _tok() > ceiling:
        after = _tok()
        log.error("EMERGENCY SHRINK: protected material exceeds ceiling (before=%d now=%d ceiling=%d)", before, after, ceiling)
        raise ContextCapacityError(
            f"Protected current-turn data alone exceeds the safe budget "
            f"({after} tokens > ceiling {ceiling}). Reduce input or increase CTXGATE_MAX_CONTEXT.",
            before, ceiling)
    after = _tok()
    log.warning("EMERGENCY SHRINK: before=%d after=%d ceiling=%d groups_dropped=%d", before, after, ceiling, dropped_groups)
    return work

async def _window_persist(session_key: str, ws: dict) -> None:
    """Upsert the window row. Feature-detects the table; a failure never fails a
    request (skips persistence for 60s after an error, warns once)."""
    global _window_persist_enabled, _window_persist_warned, _window_persist_cooldown_until
    if _window_persist_enabled is False:
        return
    if not pool:
        return
    if time.monotonic() < _window_persist_cooldown_until:
        return
    try:
        await pool.execute(
            "INSERT INTO proxy.session_windows (session_key, cut, cut_anchor, cut_prev_anchor, seed_sig, summarized_through, dropped_total, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,now()) "
            "ON CONFLICT (session_key) DO UPDATE SET cut=$2, cut_anchor=$3, cut_prev_anchor=$4, seed_sig=$5, summarized_through=$6, dropped_total=$7, updated_at=now()",
            session_key, ws["cut"], ws["cut_anchor"], ws["cut_prev_anchor"], ws["seed_sig"], ws["summarized_through"], ws["dropped_total"],
        )
        _window_persist_enabled = True
    except Exception as e:
        _window_persist_cooldown_until = time.monotonic() + 60
        metrics["window_persist_errors"] += 1
        if not _window_persist_warned:
            _window_persist_warned = True
            log.warning("session_windows persistence cooling down 60s after error: %s", e)

async def _window_load(session_key: str) -> dict:
    """Lazy load of a single window row. Returns {} if missing/absent."""
    global _window_persist_enabled, _window_persist_warned
    if _window_persist_enabled is False:
        return {}
    if not pool:
        return {}
    try:
        row = await pool.fetchrow("SELECT cut, cut_anchor, cut_prev_anchor, seed_sig, summarized_through, dropped_total FROM proxy.session_windows WHERE session_key=$1", session_key)
        _window_persist_enabled = True
        if not row:
            return {}
        return {
            "cut": row["cut"],
            "cut_anchor": row["cut_anchor"] or "",
            "cut_prev_anchor": row["cut_prev_anchor"] or "",
            "seed_sig": row["seed_sig"] or "",
            "summarized_through": row["summarized_through"],
            "dropped_total": row["dropped_total"],
        "in_flight": False
        }
    except Exception as e:
        _window_persist_enabled = False
        metrics["window_persist_errors"] += 1
        if not _window_persist_warned:
            _window_persist_warned = True
            log.warning("session_windows load disabled for this process: %s", e)
        return {}

async def _window_cleanup_ttl() -> None:
    """Delete window rows older than WINDOW_TTL_DAYS (this table only)."""
    global _window_persist_enabled, _window_persist_warned, _window_persist_cooldown_until
    if _window_persist_enabled is False or not pool:
        return
    if time.monotonic() < _window_persist_cooldown_until:
        return
    try:
        await pool.execute("DELETE FROM proxy.session_windows WHERE updated_at < (now() - ($1 || ' days')::interval)", str(WINDOW_TTL_DAYS))
    except Exception as e:
        _window_persist_cooldown_until = time.monotonic() + 60
        metrics["window_persist_errors"] += 1
        if not _window_persist_warned:
            _window_persist_warned = True
            log.warning("session_windows cleanup cooling down 60s after error: %s", e)

async def build_context(request_messages: list, task_uuid: str = None, session_key: str = None) -> list:
    sk = session_key or ""
    async with _window_lock(sk):
        ws = session_compactions.get(sk)
        if ws is None and sk and sk not in _window_load_tried:
            _window_load_tried.add(sk)
            loaded = await _window_load(sk)
            if loaded:
                ws = loaded
                session_compactions[sk] = ws
                metrics["window_loads_db"] += 1
        raw = request_messages
        seed_raw = list(raw[:3])
        rest_raw = raw[3:]
        # FAST PATH: valid sticky cut -> O(kept) only; never tokenizes the dropped region.
        if ws and _window_valid(ws, seed_raw, rest_raw) and ws["cut"] < len(rest_raw):
            combined = _prep_messages(seed_raw + rest_raw[ws["cut"]:])
            kept = combined[:3] + [{"role": "system", "content": STUB_TEXT}] + combined[3:]
            # Insert the deterministic pinned newest-user copy when it lies before the
            # cut (same as _kept_messages) so the prefix stays byte-stable between turns.
            lu = _newest_user_idx(rest_raw)
            if lu is not None and lu < ws["cut"]:
                kept.insert(4, _make_pinned_copy(rest_raw[lu], distance=ws["cut"] - lu))
            if STABLE_ELIDE:
                if ws.get("elide_idx") is None:
                    ws["elide_idx"] = _stable_elide_idx(ws["cut"], rest_raw)
                # Deterministically elide the [cut, elide_idx) tool bodies so
                # the kept window is byte-stable between requests.
                # kept = seed(3) + stub(1) + [pinned(1)] + rest[cut:]
                # so rest[cut] starts at kept[4+has_pin].
                has_pin = 1 if (lu is not None and lu < ws["cut"]) else 0
                base = 4 + has_pin
                # rest[cut:elide_idx] maps to kept[base : base+(elide_idx-cut)]
                _stable_elide_inplace(kept, base, base + (ws["elide_idx"] - ws["cut"]))
            kept_tok = count_messages_tokens(kept)
            ceiling = min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT)
            if kept_tok > ceiling:
                # Sticky window exceeded the hard ceiling: bail out of the fast
                # path and let the slow path re-cut to _trim_target(), update
                # session_compactions, and kick the summarizer. The old code
                # called _emergency_shrink here, dropping the oldest groups on
                # every request while ws["cut"] stayed fixed — the first kept
                # message changed each turn, killing the vLLM prefix cache.
                log.info("sticky window over ceiling %d > %d: re-cutting", kept_tok, ceiling)
            elif kept_tok <= MAX_INPUT:
                metrics["trim_sticky_reuse"] += 1
                log.info("Context sticky-reuse: session=%s cut=%d kept_tokens=%d skipped=%d", sk, ws["cut"], kept_tok, ws["cut"])
                # Verify context invariants (diagnostic + metrics)
                _viols = verify_context_invariants(kept, raw, ceiling)
                if _viols:
                    for _v in _viols:
                        if _v.startswith("tool_body"):
                            metrics["recent_tool_preservation_failures"] += 1
                        elif _v.startswith("newest_user") or _v.startswith("pinned"):
                            metrics["pinned_user_preservation_failures"] += 1
                    log.warning("Context invariant violations (fast path): %s", _viols[:3])
                if task_uuid and ws["summarized_through"] < ws.get("dropped_total", ws["cut"]) and not ws.get("in_flight", False):
                    la = _summary_last_attempt.get(task_uuid)
                    if la is None or (time.time() - la[0]) > 120:
                        ws["pending_cut"] = ws["cut"]
                        ws["in_flight"] = True
                        _spawn(_summarize_trimmed_messages(task_uuid, sk, rest_raw, pending_cut=ws["cut"]))
                return kept
        # SLOW PATH: no valid state (or kept over limit) -> full count + re-cut.
        messages = _prep_messages(raw)
        seed = list(messages[:3])
        rest = messages[3:]
        total = count_messages_tokens(messages)
        log.info("Context: %d messages, %d tokens (limit %d)", len(messages), total, MAX_INPUT)
        if total <= MAX_INPUT:
            if sk:
                session_compactions.pop(sk, None)
            return messages
        target = _trim_target()
        # Pre-recut elision: shrink tool bodies up to the newest-4 boundary so
        # _recut_to budgets on stable (post-elision) sizes, not full originals.
        # messages is a fresh _prep_messages list (dict copies) -> caller safe.
        _pre_elide = _stable_elide_idx(0, rest) if STABLE_ELIDE else 0
        if STABLE_ELIDE:
            # messages = seed(3) + rest; _pre_elide is a rest-index.
            # Elide rest[0:_pre_elide] -> messages[3 : 3+_pre_elide]
            _stable_elide_inplace(messages, 3, 3 + _pre_elide)
        cut = _recut(messages)
        elide_idx = max(cut, _pre_elide) if STABLE_ELIDE else 0
        # F2b: never store a window anchored past the end of rest (caused the
        # per-turn re-collapse, W2). Clamp so the anchor is a real message.
        if rest and cut >= len(rest):
            cut = len(rest) - 1
        # F3: carry the summarizer watermark forward on a re-cut of the SAME session
        # (seed unchanged, history only grew) - never reset to 0. Only a genuinely
        # new/changed seed (or missing state) starts from 0.
        st_start = 0
        if ws:
            if _window_valid(ws, seed_raw, rest_raw):
                st_start = ws.get("summarized_through", 0)
            elif ws.get("seed_sig") == _seed_sig(seed):
                st_start = ws.get("summarized_through", 0)
        new_ws = _new_window_state(cut, rest, seed, st_start)
        session_compactions[sk] = new_ws
        metrics["trim_events"] += 1
        metrics["dropped_total"] += (cut - st_start) if cut > st_start else 0
        metrics["compaction_events"] += 1
        kept = _kept_messages(seed, rest, cut)
        new_ws["elide_idx"] = elide_idx
        if STABLE_ELIDE:
            # Elide the [cut, elide_idx) tool bodies in the freshly built kept
            # window. kept = seed(3) + stub(1) + [pinned(1)] + rest[cut:]
            lu2 = _newest_user_idx(rest)
            has_pin2 = 1 if (lu2 is not None and cut > lu2) else 0
            base2 = 4 + has_pin2
            # rest[cut:elide_idx] maps to kept[base2 : base2+(elide_idx-cut)]
            _stable_elide_inplace(kept, base2, base2 + (elide_idx - cut))
        kept_tok = count_messages_tokens(kept)
        ceiling = min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT)
        if kept_tok > ceiling:
            try:
                kept = _emergency_shrink(kept, ceiling)
            except ContextCapacityError as cce:
                log.error("Context capacity exceeded in build_context: %s", cce)
                raise cce
            kept_tok = count_messages_tokens(kept)
        if kept_tok > ceiling:
            log.error("EMERGENCY SHRINK failed to meet ceiling: kept_tok=%d ceiling=%d (pathological single huge message)", kept_tok, ceiling)
        # Verify context invariants (diagnostic + metrics)
        _viols = verify_context_invariants(kept, raw, ceiling)
        if _viols:
            for _v in _viols:
                if _v.startswith("tool_body"):
                    metrics["recent_tool_preservation_failures"] += 1
                elif _v.startswith("newest_user") or _v.startswith("pinned"):
                    metrics["pinned_user_preservation_failures"] += 1
            log.warning("Context invariant violations (slow path): %s", _viols[:3])
        headroom = ceiling - kept_tok
        log.info("Context over limit: %d > %d, re-cut to low-watermark %d (%.0f%%)", total, MAX_INPUT, target, 100.0 * target / MAX_INPUT)
        log.info("After trim: %d msgs, %d tokens (headroom %d ceiling %d)", len(kept), kept_tok, headroom, ceiling)
        if kept_tok > target:
            log.info("target missed by %d tokens", kept_tok - target)
        if cut > st_start and task_uuid:
            log.info("Dropped slice: session=%s msgs=%d -> summarizer", sk, cut - st_start)
            new_ws["pending_cut"] = cut
            new_ws["in_flight"] = True
            _spawn(_summarize_trimmed_messages(task_uuid, sk, rest, pending_cut=cut))
        return kept
def _truncate_message_content(msg: dict, max_chars: int) -> dict:
    """Truncate a message's content to fit within max_chars (approximate token->char ratio 4:1)."""
    content = msg.get("content")
    if content is None:
        return msg
    if isinstance(content, str):
        if len(content) > max_chars:
            m = dict(msg)
            m["content"] = content[:max_chars] + "\n[...truncated...]"
            return m
        return msg
    if isinstance(content, list):
        # Multi-part content: truncate the text parts
        total = sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
        if total > max_chars:
            m = dict(msg)
            m["content"] = content[:1]  # keep first part only
            if isinstance(content[0], dict) and "text" in content[0]:
                m["content"] = [dict(content[0], text=content[0]["text"][:max_chars] + "\n[...truncated...]")]
            return m
        return msg
    return msg


def trim_context(messages: list, max_tokens: int) -> list:
    """FIFO trim: protect system + NEWEST user message, drop oldest first.

    Strategy:
    1. System messages always kept (truncated if necessary)
    2. The LAST user message (current request) is ALWAYS protected - never truncated
    3. Fill remaining budget from newest-to-oldest (excluding protected)
    4. If a middle message doesn't fit, truncate its content
    5. Oldest messages are dropped first
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    last_user = None
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m
            break

    keep_head = []
    if system_msgs:
        keep_head.extend(system_msgs)
    if last_user:
        keep_head.append(last_user)

    head_tokens = count_messages_tokens(keep_head)

    # If head alone exceeds budget, truncate system messages (never the last user)
    if head_tokens >= max_tokens:
        log.warning("Head alone is %d tokens (limit %d) - truncating system msgs", head_tokens, max_tokens)
        last_user_tok = count_message_tokens(last_user) if last_user else 0
        sys_budget = max(500, (max_tokens - last_user_tok - 200) // max(1, len(system_msgs))) if system_msgs else 0
        new_head = []
        for m in keep_head:
            if m is last_user:
                new_head.append(m)
            else:
                new_head.append(_truncate_message_content(m, sys_budget * 4))
        keep_head = new_head
        head_tokens = count_messages_tokens(keep_head)

    tail_budget = max_tokens - head_tokens - 100
    if tail_budget < 100:
        log.warning("Tail budget is only %d - returning head only", tail_budget)
        return keep_head

    head_ids = set(id(x) for x in keep_head)
    tail = []
    tail_tokens = 0
    for m in reversed(messages):
        if id(m) in head_ids:
            continue
        mt = count_message_tokens(m)
        if tail_tokens + mt > tail_budget:
            remaining = tail_budget - tail_tokens
            if remaining > 200:
                # Truncate to fit the REMAINING space (not total budget)
                char_budget = max(200, remaining * 3)  # 3:1 ratio (conservative)
                truncated = _truncate_message_content(m, char_budget)
                tmt = count_message_tokens(truncated)
                if tmt <= remaining:
                    tail.append( truncated)
                    tail_tokens += tmt
                    log.info("Trimmed older message to fit (was %d, now %d tokens, remaining=%d)", mt, tmt, remaining)
                else:
                    # Truncation wasn't aggressive enough - force harder cut
                    char_budget = max(100, (remaining // 2) * 3)
                    truncated = _truncate_message_content(m, char_budget)
                    tmt = count_message_tokens(truncated)
                    if tmt <= remaining:
                        tail.append( truncated)
                        tail_tokens += tmt
                        log.info("Force-trimmed older message (was %d, now %d tokens)", mt, tmt)
            break
        tail.append( m)
        tail_tokens += mt
    tail.reverse()

    # Strip leading assistant messages from tail (orphaned after trim -
    # an assistant msg with no preceding user msg breaks the chat template)
    while tail and tail[0].get("role") == "assistant":
        tail.pop(0)
    # Reorder: system first, then chronological tail, then last_user at end
    result = [m for m in keep_head if m is not last_user]
    result.extend(tail)
    if last_user:
        result.append(last_user)

    # Safety net: if result still exceeds budget, drop oldest tail messages
    total = count_messages_tokens(result)
    if total > max_tokens:
        over = total - max_tokens
        log.warning("Post-trim safety: still %d tokens over (%d > %d), dropping oldest", over, total, max_tokens)
        # Drop from the oldest non-protected messages (start of result after system msgs)
        sys_count = len([m for m in result if m.get("role") == "system"])
        while over > 0 and len(result) > sys_count + 2:  # keep system + last_user minimum
            oldest = result[sys_count]
            ot = count_message_tokens(oldest)
            result.pop(sys_count)
            over -= ot
        total = count_messages_tokens(result)
        log.info("Post-trim safety: now %d messages, %d tokens", len(result), total)

    log.info("Trimmed: %d -> %d messages (protected newest)", len(messages), len(result))
    return result

def _track_session_tokens(session_key: str, input_tokens: int, output_tokens: int = 0, count_req: bool = True):
    """Update per-session token counters. count_req=False for output-only updates."""
    global SESSION_LAST_ACTIVE
    SESSION_LAST_ACTIVE[session_key] = time.time()
    if session_key not in session_tokens:
        session_tokens[session_key] = {"in": 0, "out": 0, "reqs": 0, "max_ctx": 0}
    st = session_tokens[session_key]
    st["in"] += input_tokens
    st["out"] += output_tokens
    if count_req:
        st["reqs"] += 1
    st["last_active"] = time.strftime("%H:%M:%S", time.localtime())
    st["max_ctx"] = max(st["max_ctx"], input_tokens)

def explain_status(status: str, detail: str = "") -> str:
    """Deterministic human-readable explanation for a call status (no LLM)."""
    s = (status or "").lower()
    if not s or s == "ok":
        return ""
    if "400" in s:
        if detail:
            try:
                d = json.loads(detail)
                msg = d.get("message") if isinstance(d, dict) else None
                if msg:
                    return "vLLM HTTP 400 - " + str(msg)[:200]
            except Exception:
                pass
            return "vLLM HTTP 400 - request rejected (context / params / malformed body)"
        return "vLLM HTTP 400 - request rejected (context / params / malformed body)"
    if "404" in s:
        return "HTTP 404 - model not found on vLLM"
    if "422" in s:
        return "HTTP 422 - validation error on request body"
    if "429" in s:
        return "HTTP 429 - vLLM rate limited / overloaded"
    if "500" in s:
        return "vLLM HTTP 500 - internal server error"
    if "502" in s or "503" in s:
        return "vLLM HTTP 502/503 - upstream unavailable"
    if "504" in s or s == "timeout":
        return "Timeout - vLLM did not respond within 300s (cold APC / long generation)"
    if s == "error":
        return "Proxy error - exception while forwarding to vLLM"
    return s.replace("_", " ")

# --- Cross-session knowledge sharing ---
import re as _re


async def _call_lm_4b(prompt: str, system: str = "Return only valid JSON. No markdown, no commentary.", temperature: float = 0.3, max_tokens: int = 512, priority: int = LM_PRI_LOW, kind: str = "other") -> str:
    """Submit a Mistral API call to the priority queue. Returns raw text or empty string on failure."""
    if _lm_queue is None:
        log.debug("Mistral queue not ready, _call_lm_4b skipped")
        return ""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    result = await _call_4b(messages, max_tokens=max_tokens, json_mode=False, priority=priority, kind=kind)
    if isinstance(result, str):
        return result
    return ""






_EXTRACT_SEM = asyncio.Semaphore(2)
_EXTRACT_IN_FLIGHT = 0

async def _sync_deliverable_summary(session_id: str):
    """Background: update deliverable summaries from latest working_memory for matching session."""
    if not pool:
        return
    try:
        info = await _get_goose_session_info(session_id)
        if not info or not info.get("working_dir"):
            return
        wm = await pool.fetchrow(
            """SELECT wm.content FROM proxy.working_memory wm
               JOIN proxy.tasks t ON t.id = wm.task_id
               WHERE t.working_dir = $1
               ORDER BY wm.updated_at DESC LIMIT 1""",
            info["working_dir"]
        )
        if not wm or not wm["content"]:
            return
        summary = wm["content"][:200]
        await pool.execute(
            "UPDATE proxy.deliverables SET summary=$1, updated_at=now() WHERE working_dir=$2",
            summary, info["working_dir"]
        )
    except Exception as e:
        log.debug("deliverable summary sync failed for %s: %s", session_id, e)

def _newest_user_anchor(messages: list) -> str | None:
    """Anchor of the newest user message, ignoring <turn-context> boilerplate.

    Returns None if there is no user message, or the stripped content is shorter
    than KNOWLEDGE_EXTRACT_MIN_CHARS (trivial turns are not worth extracting).
    """
    newest = None
    for m in messages:
        if m.get("role") == "user":
            newest = m
    if newest is None:
        return None
    c = newest.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    c = str(c or "")
    c = re.sub(r"<turn-context>.*?</turn-context>", "", c, flags=re.DOTALL).strip()
    if len(c) < KNOWLEDGE_EXTRACT_MIN_CHARS:
        return None
    return _msg_anchor(newest)

async def _fire_and_forget_extract(session_id: str, session_key: str, messages: list):
    """Background knowledge extraction - never blocks the request path.

    Step 2: at most one extraction per user turn. We keep the anchor of the
    newest user message we last extracted for each session and skip when it has
    not changed. A kill switch (CTXGATE_KNOWLEDGE_EXTRACT=0) disables it.
    """
    global _EXTRACT_IN_FLIGHT
    # Kill switch (read at call time so it can be toggled via env without restart)
    if os.environ.get("CTXGATE_KNOWLEDGE_EXTRACT", "1") == "0":
        return
    # Newest-user-message anchor (None => trivial/short turn, not worth it)
    anchor = _newest_user_anchor(messages)
    if anchor is None:
        metrics["extract_skipped_short"] += 1
        return
    # Once per user turn: skip if we already extracted this exact user message
    if _last_extract_anchor.get(session_key) == anchor:
        metrics["extract_skipped_same_turn"] += 1
        return
    _last_extract_anchor[session_key] = anchor
    async with _EXTRACT_SEM:
        _EXTRACT_IN_FLIGHT += 1
        try:
            k_items = await extract_knowledge(session_id, session_key, messages)
            if k_items:
                await store_knowledge(k_items, session_id, session_key)
        except Exception as e:
            log.warning("Knowledge extraction (background) failed: %s", e)
        finally:
            _EXTRACT_IN_FLIGHT -= 1
        # Background deliverable summary sync (fire-and-forget, never blocks)
        try:
            await _sync_deliverable_summary(session_id)
        except Exception:
            pass
async def extract_knowledge(session_id: str, session_key: str, messages: list) -> list:
    """Async 4B-based knowledge extraction (single call, structural filters only).

    Simplified from the old 3-5 call pipeline (generate -> verify -> regenerate
    -> re-verify). The knowledge table was empty because the multi-call pipeline
    timed out on the single-threaded 4B. Now: one generate call + deterministic
    structural filters. Queued at LM_PRI_LOW so it never blocks trim summaries.
    """
    if not pool or not messages:
        return []
    relevant = [m for m in messages if m.get("role") in ("user", "assistant")][-6:]
    if not relevant:
        return []
    parts = []
    for m in relevant:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if c:
            parts.append(m["role"] + ": " + c[:800])
    if not parts:
        return []
    context = "\n".join(parts)

    gen_prompt = (
        "Extract 0-3 knowledge items worth preserving across sessions. "
        "Return a JSON array of {domain, key, value, importance}.\n"
        "domain: fact|decision|config|preference\n"
        "key: 2-5 descriptive words (not a single common word)\n"
        "value: 30-200 chars, a complete semantic statement (not a fragment)\n"
        "importance: 5-10\n"
        "Return [] if nothing is worth preserving.\n\n" + context
    )
    raw = await _call_lm_4b(gen_prompt, temperature=0.3, max_tokens=512, priority=LM_PRI_LOW, kind="knowledge")
    if not raw:
        return []
    try:
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    # Deterministic structural filter (no LLM quality check)
    candidates = []
    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        d = it.get("domain", "fact")
        k = (it.get("key") or "").strip().lower()
        v = (it.get("value") or "").strip()
        imp = min(10, max(1, int(it.get("importance", 5))))
        if len(k) < 4 or len(k) > 80:
            continue
        if len(v) < 20 or len(v) > 300:
            continue
        if d not in ("fact", "decision", "config", "preference"):
            d = "fact"
        candidates.append({"domain": d, "key": k, "value": v, "importance": imp})

    return candidates

async def store_knowledge(items: list, source_session: str, source_key: str) -> int:
    """Upsert knowledge items into the global knowledge table."""
    if not pool or not items:
        return 0
    stored = 0
    for item in items:
        try:
            await pool.execute(
                "INSERT INTO proxy.knowledge (domain, key, value, importance, source_session, source_key, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, now()) "
                "ON CONFLICT (domain, key) WHERE active = true "
                "DO UPDATE SET value = EXCLUDED.value, importance = GREATEST(EXCLUDED.importance, proxy.knowledge.importance), "
                "source_session = EXCLUDED.source_session, source_key = EXCLUDED.source_key, updated_at = now()",
                item["domain"], item["key"], item["value"], item.get("importance", 5), source_session, source_key
            )
            stored += 1
        except Exception as e:
            log.warning("Knowledge store failed: %s", e)
    return stored

def _extract_terms(messages: list) -> set:
    """Deterministic relevance signals from the current request (section 5)."""
    stop = {"the","and","for","with","this","that","from","have","will","your","what","when","where","which","how","can","could","would","should","about","there","here","been","being","were","was","are","is","not","all","any","but","its","you","our","their","then","than","into","over","under","also","just","only","some","such","more","most","other","out","use","using","used","make","made","get","got","one","two","see","now","new","old","set","add","run","test","tests"}
    terms = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        for w in _re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{3,}', c.lower()):
            if w not in stop:
                terms.add(w)
    return terms


def _context_blob(messages: list) -> str:
    """Lowercased text of the current Goose context (dedupe base)."""
    parts = []
    for m in messages:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if c:
            parts.append(c.lower())
    return " ".join(parts)


def _already_in_context(text: str, blob: str) -> bool:
    """True if the memory's distinctive content is already in the Goose context.
    Deterministic (no LLM): if most significant tokens are present, it is redundant."""
    stop = {"the","and","for","with","this","that","from","have","will","your","what","when","where","which","how","can","could","would","should","about","there","here","been","being","were","was","are","is","not","all","any","but","its","you","our","their"}
    toks = [w for w in _re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{3,}', text.lower()) if w not in stop]
    uniq = set(toks)
    if not uniq:
        return False
    present = sum(1 for w in uniq if w in blob)
    return present >= max(1, int(0.6 * len(uniq)))


def _score_memory(key, value, category, importance, updated_at, context_terms, context_blob):
    if len(value) > 10 and value[:50] in context_blob:
        return 0.0
    mem_terms = set(re.findall(r'[a-z0-9]{3,}', (key + ' ' + value).lower()))
    overlap = len(mem_terms & context_terms) / max(1, len(mem_terms)) if mem_terms else 0.0
    imp = importance / 10.0
    rec = 0.5
    if updated_at is not None:
        try:
            hours = max(0, (datetime.datetime.now(datetime.timezone.utc) - updated_at).total_seconds() / 3600)
            rec = math.exp(-hours / 72.0)
        except Exception:
            rec = 0.5
    dec = 1.0 if category == 'DECISION' else 0.0
    return 0.5 * overlap + 0.2 * imp + 0.1 * rec + 0.2 * dec

async def fetch_task_memory(session_id: str, messages: list, task_uuid: str = None, wm_budget: int = 1200, mem_budget: int = 3300, total_budget: int = 6000) -> str:
    """Section 13/14: per-task durable memory as a SMALL CONDITIONAL supplement.

    Final-architecture rules:
      - Inject ONLY memory not already present in the current Goose context
        (Goose compaction owns short-term continuity; we add what is missing).
      - Gate on deterministic relevance signals (section 5); no LLM in the loop.
      - If nothing relevant / already in context -> inject nothing (zero overhead).
      - Bounded to a small supplement (default <= ~2k tokens total).
      - Appended to the END of the system prompt (stable prefix, small suffix).
    Priority: working state > critical durable > relevant historical.
    """
    if not pool:
        return ""
    if task_uuid is None:
        task_uuid = await _resolve_task(session_id, create=False)
        if task_uuid is None:
            return ""
    blob = _context_blob(messages)
    terms = _extract_terms(messages)
    parts = []
    total = 0

    # 1. Working memory (current working state) - inject only if not already in context
    # 1+1b+2. Working memory + session summary + durable memories (parallel)
    async def _fetch_rel_mem():
        if terms:
            rows = await pool.fetch(
                "SELECT id, key, value, category, importance, updated_at FROM proxy.memories "
                "WHERE task_id=$1 AND active=true "
                "AND (key ILIKE ANY($2) OR value ILIKE ANY($2)) "
                "LIMIT 30",
                task_uuid, ["%" + t + "%" for t in list(terms)[:20]]
            )
            scored = []
            for r in rows:
                s = _score_memory(r["key"], r["value"], r.get("category", "general"), r.get("importance", 5), r.get("updated_at"), terms, blob)
                if s > 0.1:
                    scored.append((s, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [r for _, r in scored[:8]]
        return []

    wrow, summary, crit, rel = await asyncio.gather(
        pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_uuid),
        _fetch_session_summary(task_uuid, budget=1500, session_key=session_id),
        pool.fetch("SELECT id, key, value, category, importance, updated_at FROM proxy.memories WHERE task_id=$1 AND active=true AND importance=10 ORDER BY updated_at DESC LIMIT 5", task_uuid),
        _fetch_rel_mem(),
        return_exceptions=True
    )
    if isinstance(wrow, Exception):
        log.warning("Working memory fetch failed: %s", wrow)
        wrow = None
    if isinstance(summary, Exception):
        log.warning("Session summary fetch failed: %s", summary)
        summary = ""
    if isinstance(crit, Exception):
        log.warning("Critical memory fetch failed: %s", crit)
        crit = []
    if isinstance(rel, Exception):
        log.warning("Relevant memory fetch failed: %s", rel)
        rel = []

    wm_text = (wrow["content"].strip() if wrow and wrow["content"] else "")
    if wm_text and not _already_in_context(wm_text, blob):
        line = "WORKING MEMORY: " + wm_text
        t = count_tokens(line)
        if t <= wm_budget and total + t <= total_budget:
            parts.append(line)
            total += t

    if summary and not _already_in_context(summary, blob):
        line = ("TASK STATE (background only; the NEWEST user message is the live instruction - never resume the first prompt unless the newest message asks for it. "
                "Where an older message says something is not yet done but this state or the files on disk say it is, trust this state and the disk): " + summary)
        t = count_tokens(line)
        if t <= 1500 and total + t <= total_budget:
            parts.append(line)
            total += t

    rows = list(crit) + list(rel)

    seen = set()
    injected_ids: list = []
    for row in rows:
        line = row["key"] + ": " + row["value"]
        nk = _re.sub(r'[^a-z0-9]+', ' ', line.lower()).strip()
        if nk in seen:
            continue
        if _already_in_context(line, blob):
            continue
        if wm_text and row["value"] in wm_text:
            continue
        t = count_tokens(line)
        if total + t > min(mem_budget, total_budget):
            break
        parts.append(line)
        injected_ids.append(row["id"])
        seen.add(nk)
        total += t

    # Touch-on-use: stamp last_accessed_at + score
    try:
        if injected_ids:
            await pool.execute("UPDATE proxy.memories SET last_accessed_at = now(), score = GREATEST(COALESCE(score, 0), 0.5) WHERE id = ANY($1)", injected_ids)
    except Exception as e:
        log.warning("touch-on-use update failed: %s", e)

    if not parts:
        return ""
    return "[Task Memory]\n" + "\n".join(parts)


async def fetch_relevant_knowledge(messages: list, max_items: int = 5, max_tokens: int = 500) -> str:
    """Fetch top-N most relevant knowledge items. Returns formatted text for system prompt injection."""
    if not pool:
        return ""
    search_terms = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        for word in _re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]{3,}\b', content.lower()):
            if word not in ("the","and","for","with","this","that","from","have","will","your","what","when","where","which","how","can","could","would","should","about","there","here","been","being","were","was","are","is","not","all","any","but","its","you","our","their"):
                search_terms.add(word)
    rows = []
    try:
        if search_terms:
            terms_list = list(search_terms)[:20]
            rows = await pool.fetch(
                "SELECT key, value, importance FROM proxy.knowledge WHERE active = true "
                "AND (key ILIKE ANY($1) OR value ILIKE ANY($1)) "
                "ORDER BY importance DESC, updated_at DESC LIMIT $2",
                [f"%{t}%" for t in terms_list], max_items
            )
        if not rows:
            rows = await pool.fetch(
                "SELECT key, value, importance FROM proxy.knowledge WHERE active = true "
                "ORDER BY importance DESC, updated_at DESC LIMIT $1", max_items
            )
    except Exception:
        return ""
    if not rows:
        return ""
    parts = []
    total_tokens = 0
    for row in rows:
        line = row['key'] + ": " + row['value']
        tok = count_tokens(line)
        if total_tokens + tok > max_tokens:
            break
        parts.append(line)
        total_tokens += tok
    if not parts:
        return ""
    return "[Shared Knowledge]\n" + "\n".join(parts)


def _record_call(session_key: str, input_tokens: int, output_tokens: int, status: str, model: str, stream: bool, detail: str = "", cached_tokens: int = 0):
    """Add to ring buffer of recent calls."""
    entry = {
        "ts": time.time(),
        "ts_human": time.strftime("%H:%M:%S", time.localtime()),
        "session": session_key,
        "in": input_tokens,
        "out": output_tokens,
        "cached": cached_tokens,
        "cached_pct": round(100 * cached_tokens / max(1, input_tokens), 1),
        "status": status,
        "model": model,
        "stream": stream,
        "detail": (detail or "")[:300],
        "explanation": explain_status(status, detail),
    }
    recent_calls.append(entry)

# --- Health endpoint ---
def _fd_breakdown() -> dict:
    """Classify this process's open file descriptors by type, plus TCP socket
    states (CLOSE_WAIT called out). Reads /proc/self/fd (readlink) and
    /proc/self/net/tcp{,6}. Used by /health and the fd hygiene loop so the
    leaking *category* is visible, not just the total.
    """
    import os as _os
    by_type = {"socket": 0, "pipe": 0, "anon_inode": 0, "file": 0, "other": 0}
    total = 0
    try:
        for _fd in _os.listdir("/proc/self/fd"):
            total += 1
            try:
                _t = _os.readlink("/proc/self/fd/" + _fd)
            except OSError:
                by_type["other"] += 1
                continue
            if _t.startswith("socket:"):
                by_type["socket"] += 1
            elif _t.startswith("pipe:"):
                by_type["pipe"] += 1
            elif _t.startswith("anon_inode:"):
                by_type["anon_inode"] += 1
            elif _t.startswith("/") or _t.startswith("deleted"):
                by_type["file"] += 1
            else:
                by_type["other"] += 1
    except Exception:
        pass
    # TCP state codes: 01 ESTABLISHED, 06 TIME_WAIT, 08 CLOSE_WAIT, 0A LISTEN
    _tcp_names = {"01":"ESTABLISHED","06":"TIME_WAIT","08":"CLOSE_WAIT","0A":"LISTEN",
                  "07":"FIN_WAIT1","09":"CLOSING","04":"SYN_SENT","05":"SYN_RECV",
                  "02":"SYN_RECV","03":"FIN_WAIT2","0B":"LAST_ACK","0C":"LISTEN","0D":"CLOSED"}
    tcp_states = {}
    close_wait = 0
    for _p in ("/proc/self/net/tcp", "/proc/self/net/tcp6"):
        try:
            with open(_p) as _f:
                next(_f, None)
                for _line in _f:
                    _parts = _line.split()
                    if len(_parts) < 4:
                        continue
                    _st = _parts[3]
                    _nm = _tcp_names.get(_st, _st)
                    tcp_states[_nm] = tcp_states.get(_nm, 0) + 1
                    if _st == "08":
                        close_wait += 1
        except Exception:
            pass
    try:
        import resource
        _limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        _limit = -1
    return {"total": total, "limit": _limit, "by_type": by_type, "tcp_states": tcp_states, "close_wait": close_wait}

@app.get("/health")
async def health():
    import os as _os
    try:
        _fd_count = len(_os.listdir("/proc/self/fd"))
    except Exception:
        _fd_count = -1
    return {"status": "ok", "version": "1.0.0", "sessions": len(session_fingerprints), "fd_count": _fd_count, "fd": _fd_breakdown()}

# --- Metrics endpoints ---
@app.get("/metrics")
async def get_metrics():
    uptime = time.time() - metrics["started_at"]
    result = dict(metrics)
    result["uptime_human"] = _human_time(uptime)
    result["started_human"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(metrics["started_at"]))
    result["lm_rate_limiter"] = _lm_rate_limiter.stats
    result["active_sessions"] = len(session_fingerprints)
    result["recent_calls_count"] = len(recent_calls)
    return result

@app.get("/ready")
async def ready():
    checks = {}
    ok = True
    try:
        if pool:
            await pool.fetchval("SELECT 1")
            checks["postgres"] = "ok"
        else:
            checks["postgres"] = "no pool"
            ok = False
    except Exception as e:
        checks["postgres"] = "error: " + str(e)[:80]
        ok = False
    if enc:
        checks["tokenizer"] = "ok"
    else:
        checks["tokenizer"] = "missing"
        ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(VLLM_URL + "/models")
            checks["vllm"] = "ok" if r.status_code == 200 else "http_" + str(r.status_code)
            if r.status_code != 200:
                ok = False
    except Exception:
        checks["vllm"] = "unreachable"
        ok = False
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

@app.get("/metrics/prometheus")
async def metrics_prometheus():
    uptime = time.time() - metrics["started_at"]
    _worker_pending = await _worker_pending_count()
    lines = [
        "# TYPE ctxgate_requests_total counter", "ctxgate_requests_total %d" % metrics["requests_total"],
        "# TYPE ctxgate_requests_ok counter", "ctxgate_requests_ok %d" % metrics["requests_ok"],
        "# TYPE ctxgate_requests_error counter", "ctxgate_requests_error %d" % metrics["requests_error"],
    "# TYPE ctxgate_cached_tokens_total counter", "ctxgate_cached_tokens_total %d" % metrics["cached_tokens_total"],
    "# TYPE ctxgate_prompt_tokens_total counter", "ctxgate_prompt_tokens_total %d" % metrics["prompt_tokens_total"],
    "# TYPE ctxgate_cache_hit_rate gauge", "ctxgate_cache_hit_rate %.4f" % (metrics["cached_tokens_total"] / max(1, metrics["prompt_tokens_total"])),
    "# TYPE ctxgate_effective_prefill_per_request gauge", "ctxgate_effective_prefill_per_request %.1f" % ((metrics["prompt_tokens_total"] - metrics["cached_tokens_total"]) / max(1, metrics["requests_ok"])),
    "# TYPE ctxgate_evicted_sessions_total counter", "ctxgate_evicted_sessions_total %d" % metrics["evicted_sessions"],
    "# TYPE ctxgate_trim_events counter", "ctxgate_trim_events %d" % metrics["trim_events"],
    "# TYPE ctxgate_window_collapse_guard counter", "ctxgate_window_collapse_guard %d" % metrics["window_collapse_guard"],
    "# TYPE ctxgate_trim_sticky_reuse counter", "ctxgate_trim_sticky_reuse %d" % metrics["trim_sticky_reuse"],
    "# TYPE ctxgate_emergency_shrink_total counter", "ctxgate_emergency_shrink_total %d" % metrics["emergency_shrink_total"],
    "# TYPE ctxgate_emergency_shrink_groups_dropped counter", "ctxgate_emergency_shrink_groups_dropped %d" % metrics["emergency_shrink_groups_dropped"],
    "# TYPE ctxgate_dropped_total counter", "ctxgate_dropped_total %d" % metrics["dropped_total"],
    "# TYPE ctxgate_summary_slices_ok counter", "ctxgate_summary_slices_ok %d" % metrics["summary_slices_ok"],
    "# TYPE ctxgate_summary_slices_failed counter", "ctxgate_summary_slices_failed %d" % metrics["summary_slices_failed"],
    "# TYPE ctxgate_window_loads_db counter", "ctxgate_window_loads_db %d" % metrics["window_loads_db"],
    "# TYPE ctxgate_window_persist_errors counter", "ctxgate_window_persist_errors %d" % metrics["window_persist_errors"],
        "# TYPE ctxgate_prefix_invalidations counter", "ctxgate_prefix_invalidations %d" % metrics["prefix_invalidations"],
        "# TYPE ctxgate_toolcall_strips counter", "ctxgate_toolcall_strips %d" % metrics["toolcall_strips"],
        "# TYPE ctxgate_tokens_in_total counter", "ctxgate_tokens_in_total %d" % metrics["tokens_in_total"],
        "# TYPE ctxgate_tokens_out_total counter", "ctxgate_tokens_out_total %d" % metrics["tokens_out_total"],
        "# TYPE ctxgate_max_context_seen gauge", "ctxgate_max_context_seen %d" % metrics["max_context_seen"],
        "# TYPE ctxgate_active_sessions gauge", "ctxgate_active_sessions %d" % len(session_fingerprints),
        "# TYPE ctxgate_uptime_seconds gauge", "ctxgate_uptime_seconds %.1f" % uptime,
        "# TYPE ctxgate_extract_queue_depth gauge", "ctxgate_extract_queue_depth %d" % _EXTRACT_IN_FLIGHT,
        "# TYPE ctxgate_worker_lag_seconds gauge", "ctxgate_worker_lag_seconds %.1f" % _read_worker_status().get("lag_seconds", 0),
        "# TYPE ctxgate_worker_pending gauge", "ctxgate_worker_pending %d" % _worker_pending,
    ]
    # LM token usage per call kind (knowledge/phase/root/other x prompt/completion)
    lines.extend([
        "# TYPE ctxgate_lm_tokens_total counter",
        *(('ctxgate_lm_tokens_total{kind="%s",direction="%s"} %d' % (k, d, v))
          for k in ("knowledge", "phase", "root", "other")
          for d, v in metrics["lm_tokens_by_kind"].get(k, {}).items()),
        "# TYPE ctxgate_lm_calls_total counter",
        *((('ctxgate_lm_calls_total{kind="%s"} %d' % (k, v))
          for k, v in metrics["lm_calls_by_kind"].items())),
    ])
    return Response("\n".join(lines) + "\n", media_type="text/plain")

@app.get("/api/metrics")
async def api_metrics():
    """Structured metrics for dashboard (JSON)."""
    _worker_pending = await _worker_pending_count()
    uptime = time.time() - metrics["started_at"]
    _hit_rate = metrics["cached_tokens_total"] / max(1, metrics["prompt_tokens_total"])
    _eff_prefill = (metrics["prompt_tokens_total"] - metrics["cached_tokens_total"]) / max(1, metrics["requests_ok"])
    return {
        **metrics,
        "cache_hit_rate": round(_hit_rate, 4),
        "effective_prefill_per_request": round(_eff_prefill, 1),
        "uptime_sec": round(uptime, 1),
        "uptime_human": _human_time(uptime),
        "started_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(metrics["started_at"])),
        "active_sessions": len(session_fingerprints),
        "worker": _read_worker_status(),
        "worker_pending": _worker_pending,
    }

@app.get("/api/sessions")
async def api_sessions():
    """Per-session token accounting."""
    sessions = []
    for key, st in session_tokens.items():
        fp = session_fingerprints.get(key, "")
        sessions.append({
            "key": key,
            "provider": key.split(":")[0] if ":" in key else key,
            "fingerprint": fp,
            "tokens_in": st["in"],
            "tokens_out": st["out"],
            "requests": st["reqs"],
            "max_context": st["max_ctx"],
            "last_active": st.get("last_active", ""),
        })
    sessions.sort(key=lambda s: s["requests"], reverse=True)
    return sessions

@app.get("/api/recent-calls")
async def api_recent_calls(n: int = 50):
    """Recent call ring buffer."""
    items = list(recent_calls)
    return list(reversed(items[-n:]))

@app.get("/api/errors")
async def api_errors(n: int = 20):
    """Recent errors from ring buffer."""
    errs = [c for c in recent_calls if c["status"] != "ok"]
    return list(reversed(errs[-n:]))

@app.get("/api/memory-summary")
async def api_memory_summary():
    """Summary of tasks and working memory from DB."""
    if not pool:
        return {"tasks": [], "memory_entries": 0}
    try:
        tasks = await pool.fetch("SELECT session_id, created_at, updated_at FROM proxy.tasks ORDER BY updated_at DESC LIMIT 50")
        mem_count = await pool.fetchval("SELECT COUNT(*) FROM proxy.working_memory")
        result = {
            "tasks": [
                {
                    "session_id": t["session_id"],
                    "created": str(t["created_at"]),
                    "updated": str(t["updated_at"]),
                }
                for t in tasks
            ],
            "memory_entries": mem_count,
        }
        return result
    except Exception as e:
        log.warning("memory-summary query failed: %s", e)
        return {"tasks": [], "memory_entries": 0, "error": str(e)}

# --- Test helpers ---
@app.post("/_test/reset_prefix")
async def test_reset_prefix():
    global session_fingerprints
    session_fingerprints.clear()
    metrics["prefix_invalidations"] = 0
    return {"status": "reset"}

@app.post("/_test/reset_sessions")
async def test_reset_sessions():
    global session_fingerprints, session_tokens, recent_calls, metrics
    session_fingerprints.clear()
    session_tokens.clear()
    recent_calls.clear()
    metrics["prefix_invalidations"] = 0
    metrics["trim_events"] = 0
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

# --- Main chat completions endpoint ---
@app.get("/v1/models")
async def v1_models():
    """OpenAI-compatible /v1/models passthrough to vLLM.
    Prevents 404 when clients (dashboards, health checkers, Goose)
    probe the proxy with the standard OpenAI models endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(VLLM_URL.rstrip("/") + "/models")
            if resp.status_code == 200:
                return resp.json()
            return {"object": "list", "data": []}
    except Exception:
        return {"object": "list", "data": []}

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    global _inflight_count, _shutting_down
    _inflight_count += 1
    try:
        if _shutting_down:
            return JSONResponse({"error": "shutting down"}, status_code=503)
        global metrics
        if API_KEY:
            if request.headers.get("Authorization", "") != "Bearer " + API_KEY:
                return JSONResponse({"error": {"message": "Unauthorized"}}, status_code=401)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            metrics["requests_error"] += 1
            return JSONResponse({"error": {"message": "Request body too large"}}, status_code=413)
        metrics["requests_total"] += 1
        try:
            body = await request.json()
        except Exception:
            metrics["requests_error"] += 1
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        model = body.get("model", "")
        if model and model != VLLM_MODEL:
            metrics["requests_error"] += 1
            return JSONResponse({"error": {"message": "Unknown model: " + model}}, status_code=404)
        messages = body.get("messages", [])
        if not messages:
            metrics["requests_error"] += 1
            return JSONResponse({"error": "No messages provided"}, status_code=400)
        for _mi, _mm in enumerate(messages):
            if _mm.get("content") is None and _mm.get("role") != "assistant":
                metrics["requests_error"] += 1
                return JSONResponse({"error": {"message": "messages[" + str(_mi) + "].content is null - must be a string"}}, status_code=400)

        # --- Session key: provider identity + content fingerprint ---
        x_sid = request.headers.get('X-Session-ID') or await _get_goose_session_id()
        session_key = make_session_key(x_sid, messages)

        # Memory job (uses provider-level session id, not content-scoped)
        last_user_content = ''
        for m in reversed(messages):
            if m.get('role') == 'user':
                uc = m.get('content', '')
                if isinstance(uc, list):
                    last_user_content = ' '.join(p.get('text', '') for p in uc if isinstance(p, dict))
                else:
                    last_user_content = uc or ''
                break
        if last_user_content:
            _spawn(_enqueue_memory_job(x_sid, last_user_content))

        # Build context
        # Resolve task for memory/summary purposes
        task_uuid = None
        try:
            task_uuid = await _resolve_task(x_sid, create=True)
        except Exception:
            pass

        _t0 = time.monotonic()
        try:
            built = await build_context(messages, task_uuid=task_uuid, session_key=session_key)
        except ContextCapacityError as cce:
            metrics["requests_error"] += 1
            log.error("Context capacity: %s", cce)
            return JSONResponse({"error": {"message": str(cce), "explanation": "Protected current-turn data exceeds the safe context budget. Reduce input size or increase CTXGATE_MAX_CONTEXT."}, "ctxgate": ctxgate_meta(True, "context_capacity", 0, 0, False, 0)["ctxgate"]}, status_code=413)
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: build_context %.0fms (msgs=%d)", _dt, len(messages))


        # Phase 1: Freeze seed pair [0][1][2] per session.
        # First 3 messages (system + seed user + seed assistant) are immutable for session lifetime.
        # If Goose compacts and changes them, we detect it, log a deliberate miss, and re-freeze.
        if session_key not in session_seeds:
            if len(built) >= 3:
                session_seeds[session_key] = [dict(m) for m in built[:3]]
                log.info("Seed frozen session=%s (3 msgs)", session_key)
        else:
            frozen = session_seeds[session_key]
            for i, fm in enumerate(frozen):
                if i < len(built):
                    fc = fm.get("content", "")
                    nc = built[i].get("content", "")
                    if isinstance(fc, list):
                        fc = " ".join(p.get("text", "") for p in fc if isinstance(p, dict))
                    if isinstance(nc, list):
                        nc = " ".join(p.get("text", "") for p in nc if isinstance(p, dict))
                    if fc != nc:
                        metrics["prefix_invalidations"] += 1
                        log.warning("SEED CHANGED session=%s pos=%d - re-freezing (deliberate miss)", session_key, i)
                        session_seeds[session_key] = [dict(m) for m in built[:3]]
                        session_compactions.pop(session_key, None)
                        break
        # Safe defaults: guard _record_injection against a fetch exception leaving tm/kn unset
        tm = ""
        kn = ""

        # --- Knowledge + memory injection (parallel) ---
        _t0 = time.monotonic()
        kn, tm = await asyncio.gather(
            fetch_relevant_knowledge(messages, max_items=5, max_tokens=400),
            fetch_task_memory(x_sid, built, task_uuid=task_uuid),
            return_exceptions=True
        )
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: knowledge+memory fetch %.0fms", _dt)
        # Phase 1: kn/tm are APPENDED as separate messages after history, before current turn.
        # The system prompt is NEVER mutated — prefix stays byte-identical for cache stability.
        if isinstance(kn, Exception):
            log.warning("Knowledge injection failed: %s", kn)
            kn = ""
        if isinstance(tm, Exception):
            log.warning("Task memory injection failed: %s", tm)
            tm = ""
        if kn or tm:
            last_user_idx = len(built) - 1
            for i in range(len(built) - 1, -1, -1):
                if built[i].get("role") == "user":
                    last_user_idx = i
                    break
            # FIX: refuse to inject if the live user message was lost in the cut
            _raw_last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if _raw_last_user is not None and _msg_anchor(built[last_user_idx]) != _msg_anchor(_raw_last_user):
                log.error("Newest user message missing from built context (session=%s) - skipping memory injection", session_key)
                metrics["inject_skipped_stale_user"] += 1
                kn = tm = ""
        if kn or tm:
            _ctx_parts = []
            if kn:
                _ctx_parts.append("Relevant knowledge:" + chr(10) + kn)
            if tm:
                _ctx_parts.append("Task memory:" + chr(10) + tm)
            if _ctx_parts:
                # Append to last user msg content (not a separate message) to prevent
                # the model from treating it as a standalone turn to acknowledge.
                _block = chr(10) + chr(10) + chr(10).join(_ctx_parts)
                existing = built[last_user_idx].get("content", "")
                if isinstance(existing, str):
                    built[last_user_idx]["content"] = existing + _block
                else:
                    built[last_user_idx]["content"] = list(existing) + [{"type": "text", "text": _block}]
                log.debug("Memory blocks appended to last user msg at pos %d (kn=%dch tm=%dch)", last_user_idx, len(kn), len(tm))

        # --- Injection / utilization instrumentation (lightweight, no DB) ---
        _record_injection(x_sid, tm, kn)

        # Phase 2: repair dangling tool calls before fingerprint + vLLM body
        built = _repair_dangling_tool_calls(built)

        # Per-session prefix check (single hash)
        fp = hashlib.sha256(_prefix_raw(built).encode()).hexdigest()[:16]
        prev_fp = session_fingerprints.get(session_key)
        if prev_fp is not None and fp != prev_fp:
            metrics["prefix_invalidations"] += 1
            log.warning("PREFIX INVALIDATED session=%s", session_key)
        session_fingerprints[session_key] = fp


        # Token tracking
        _t0 = time.monotonic()
        input_tokens = count_messages_tokens(built)
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: count_messages_tokens %.0fms (tokens=%d)", _dt, input_tokens)
        metrics["max_context_seen"] = max(metrics.get("max_context_seen", 0), input_tokens)
        metrics["tokens_in_total"] += input_tokens
        _track_session_tokens(session_key, input_tokens)

        # --- Cross-session knowledge extraction (fire-and-forget, non-blocking) ---
        if _EXTRACT_IN_FLIGHT < 2:
            _spawn(_fire_and_forget_extract(x_sid, session_key, messages))
        else:
            metrics["extract_shed"] = metrics.get("extract_shed", 0) + 1

        # Proxy calculates output budget from post-trim input (authoritative)
        # Goose's max_tokens is based on pre-trim input - ignore it
        max_tokens = _output_budget(input_tokens)
        # Hard output floor: if the input ate the budget, shrink the window once more;
        # if it STILL can't reach MIN_OUTPUT, refuse with a clear 413-style error
        # instead of sending a starved request (the original output-starvation bug).
        if max_tokens < MIN_OUTPUT:
            ceiling = min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT)
            if input_tokens > ceiling:
                built = _emergency_shrink(built, ceiling)
                input_tokens = count_messages_tokens(built)
                max_tokens = _output_budget(input_tokens)
        if max_tokens < MIN_OUTPUT:
            log.error("Context too large for required output budget: session=%s input=%d max_tokens=%d < MIN_OUTPUT=%d", session_key, input_tokens, max_tokens, MIN_OUTPUT)
            return JSONResponse({"error": {"message": "context too large for required output budget", "input_tokens": input_tokens, "max_tokens": max_tokens, "min_output": MIN_OUTPUT}}, status_code=413)
        if input_tokens > 0.9 * MAX_INPUT:
            log.info("Budget: session=%s input=%d ceiling=%d max_tokens=%d", session_key, input_tokens, min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT), max_tokens)
        stream = body.get("stream", False)
        vllm_body = {
            "model": VLLM_MODEL,
            "messages": built,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        # Sampling: send temperature ONLY when the client sent one and it is at/above
        # the floor. Otherwise omit it so the server's tuned default (1.0) applies.
        # A near-greedy temperature in thinking mode causes endless repetition.
        _ct = body.get("temperature")
        if _ct is not None:
            if _ct < 0:
                # Negative temperature is invalid - forward it so vLLM rejects it (400).
                vllm_body["temperature"] = _ct
            elif _ct >= MIN_TEMPERATURE:
                vllm_body["temperature"] = _ct
            else:
                # 0 <= _ct < MIN_TEMPERATURE: near-greedy causes repetition in thinking
                # mode. Omit it so the server's tuned default (1.0) applies.
                log.info("Dropping client temperature %s (< MIN_TEMPERATURE %s); server default 1.0 applies", _ct, MIN_TEMPERATURE)
        if PRESENCE_PENALTY:
            vllm_body["presence_penalty"] = float(PRESENCE_PENALTY)
        if REPETITION_DETECTION:
            vllm_body["repetition_detection"] = {"max_pattern_size": 50, "min_pattern_size": 5, "min_count": 6}
        if THINKING_TOKEN_BUDGET:
            vllm_body["thinking_token_budget"] = int(THINKING_TOKEN_BUDGET)
        if stream:
            vllm_body["stream_options"] = {"include_usage": True}
        if body.get("tools"):
            vllm_body["tools"] = body["tools"]
        if body.get("tool_choice"):
            vllm_body["tool_choice"] = body["tool_choice"]
        _prefix_diag(session_key, _normalize_system_messages(vllm_body.get("messages", [])))

        _t0 = time.monotonic()
        if stream:
            result = await stream_to_vllm(vllm_body, input_tokens, session_key)
        else:
            result = await forward_to_vllm(vllm_body, input_tokens, session_key)
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: vllm_call %.0fms (in=%d stream=%s)", _dt, input_tokens, stream)
        return result
    finally:
        _inflight_count -= 1


def _read_worker_status() -> dict:
    """Read worker/.worker_status.json if it exists."""
    import json as _json
    path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker", ".worker_status.json"))
    try:
        with open(path, "r") as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        return {"alive": False, "lag_seconds": 0, "heartbeat": ""}


async def _worker_pending_count() -> int:
    """Return pending memory_jobs count, cached for WORKER_PENDING_TTL seconds.

    The hot path (per-request backpressure check in _enqueue_memory_job) must
    not run a full COUNT(*) against proxy.memory_jobs on every request. This
    helper returns the last-known value if it is fresh, and only queries the
    DB when the cache has expired or has never been populated.
    """
    now = time.time()
    if now - WORKER_PENDING_CACHE["ts"] < WORKER_PENDING_TTL:
        return WORKER_PENDING_CACHE["value"]
    try:
        if pool:
            v = await pool.fetchval(
                "SELECT COUNT(*) FROM proxy.memory_jobs WHERE status='pending'"
            )
            WORKER_PENDING_CACHE["value"] = int(v or 0)
            WORKER_PENDING_CACHE["ts"] = now
    except Exception as e:
        log.warning("worker_pending_count refresh failed: %s", e)
    return WORKER_PENDING_CACHE["value"]


async def _evict_stale_sessions():
    """Evict session state older than SESSION_TTL_HOURS."""
    await _window_cleanup_ttl()
    now = time.time()
    ttl_sec = SESSION_TTL_HOURS * 3600
    global metrics
    stale = [k for k, ts in SESSION_LAST_ACTIVE.items() if now - ts > ttl_sec]
    for k in stale:
        session_fingerprints.pop(k, None)
        _last_extract_anchor.pop(k, None)
        session_seeds.pop(k, None)
        session_compactions.pop(k, None)
        session_tokens.pop(k, None)
        SESSION_LAST_ACTIVE.pop(k, None)
        session_prefix_hashes.pop(k, None)
        _window_locks.pop(k, None)
        _window_load_tried.discard(k)
    if stale:
        metrics["evicted_sessions"] += len(stale)
        log.info("Evicted %d stale sessions (TTL %dh)", len(stale), SESSION_TTL_HOURS)


async def _vllm_health_loop():
    """Ping vLLM /models every 60s to track availability."""
    global _hygiene_task
    global vllm_alive
    while True:
        try:
            if _hygiene_task is not None and _hygiene_task.done() and not _hygiene_task.cancelled():
                _exc = _hygiene_task.exception()
                log.critical("FD hygiene task DIED: %s. Recreating.", _exc)
                _hygiene_task = asyncio.create_task(_fd_hygiene_loop())
        except Exception as _e:
            log.warning("Hygiene supervisor check failed: %s", _e)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(VLLM_URL + "/models")
                if resp.status_code == 200:
                    if not vllm_alive:
                        log.info("vLLM is BACK (was dead)")
                    vllm_alive = True
                else:
                    if vllm_alive:
                        log.warning("vLLM returned %d - marking dead", resp.status_code)
                    vllm_alive = False
        except Exception as e:
            if vllm_alive:
                log.warning("vLLM unreachable: %s - marking dead", e)
            vllm_alive = False
        try:
            await _evict_stale_sessions()
        except Exception as e:
            log.warning("_evict_stale_sessions failed: %s", e)
        try:
            await _worker_pending_count()
        except Exception as e:
            log.warning("_worker_pending_count warmup failed: %s", e)
        await asyncio.sleep(60)


# --- FD hygiene: module-level constants & state ---
FD_WARN_FRAC = 0.25
FD_RECYCLE_FRAC = 0.50
FD_RESTART_FRAC = 0.75
FD_DRAIN_DELAY = 60  # seconds to drain old client before closing
FD_RESTART_DRAIN = 30  # max seconds to wait for in-flight before exit
_shutting_down = False
_inflight_count = 0

def _get_vllm_client():
    """Accessor: return the current vLLM httpx client (safe across swaps)."""
    return _vllm_client

def _get_lm_client():
    """Accessor: return the current LM/Mistral httpx client (safe across swaps)."""
    return _lm_client

def _make_vllm_client():
    """Build a fresh vLLM httpx client with bounded pool."""
    t = httpx.Timeout(
        _env_int("CTXGATE_VLLM_READ_TIMEOUT", 300),
        connect=_env_int("CTXGATE_VLLM_CONNECT_TIMEOUT", 10),
        write=_env_int("CTXGATE_VLLM_WRITE_TIMEOUT", 120),
        pool=_env_int("CTXGATE_VLLM_POOL_TIMEOUT", 30),
    )
    return httpx.AsyncClient(timeout=t, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))

def _make_lm_client():
    """Build a fresh LM/Mistral httpx client with bounded pool."""
    t = httpx.Timeout(MISTRAL_TIMEOUT, connect=10)
    return httpx.AsyncClient(timeout=t, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                             headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"} if MISTRAL_API_KEY else {})

async def _fd_hygiene_loop():
    """Monitor fd count and actively prevent exhaustion.

    Thresholds are RELATIVE to the actual RLIMIT_NOFILE soft limit:
      - 25%: log WARNING + fd_breakdown()
      - 50%: SWAP-THEN-DRAIN httpx clients (build new, swap ref, drain old after 60s)
      - 75%: graceful restart (reject new, drain in-flight up to 30s, os._exit(1))

    The loop body is wrapped in try/except so it never dies silently.
    A supervisor check in the health loop recreates the task if it dies.
    """
    global _vllm_client, _lm_client, _shutting_down
    import os as _os
    import resource

    while True:
        try:
            await asyncio.sleep(60)

            # Compute thresholds from the actual soft limit
            soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            warn_at = int(soft_limit * FD_WARN_FRAC)
            recycle_at = int(soft_limit * FD_RECYCLE_FRAC)
            restart_at = int(soft_limit * FD_RESTART_FRAC)

            fd_count = len(_os.listdir("/proc/self/fd"))
            breakdown = _fd_breakdown()

            if fd_count >= restart_at:
                log.critical("FD RESTART: %d fds >= %d (75%% of limit %d). Graceful restart.",
                             fd_count, restart_at, soft_limit)
                log.critical("FD breakdown at restart: %s", breakdown)
                # Stop accepting new work
                _shutting_down = True
                # Wait for in-flight to drain (up to FD_RESTART_DRAIN seconds)
                for _ in range(FD_RESTART_DRAIN * 10):
                    if _inflight_count <= 0:
                        break
                    await asyncio.sleep(0.1)
                log.critical("FD RESTART: in-flight=%d, exiting.", _inflight_count)
                _os._exit(1)

            elif fd_count >= recycle_at:
                log.warning("FD RECYCLE: %d fds >= %d (50%% of limit %d). Swap-then-drain.",
                            fd_count, recycle_at, soft_limit)
                log.warning("FD breakdown at recycle: %s", breakdown)
                # SWAP-THEN-DRAIN: build new clients first
                new_vllm = _make_vllm_client()
                new_lm = _make_lm_client()
                # Atomically swap the global references
                old_vllm = _vllm_client
                old_lm = _lm_client
                _vllm_client = new_vllm
                _lm_client = new_lm
                log.info("FD recycle: clients swapped. Draining old clients in %ds.", FD_DRAIN_DELAY)
                # Schedule old client close after drain delay
                async def _drain_old(ov=old_vllm, ol=old_lm):
                    await asyncio.sleep(FD_DRAIN_DELAY)
                    for c in (ov, ol):
                        if c is not None:
                            try:
                                await c.aclose()
                            except Exception as e:
                                log.debug("FD drain: aclose failed: %s", e)
                asyncio.create_task(_drain_old())

            elif fd_count >= warn_at:
                log.warning("FD WARN: %d fds >= %d (25%% of limit %d). breakdown=%s",
                            fd_count, warn_at, soft_limit, breakdown)

        except Exception as e:
            log.exception("FD hygiene loop error (continuing): %s", e)

def _normalize_system_messages(messages):
    if not messages:
        return messages
    # Never convert a non-system slot into a system one.
    if messages[0].get("role") != "system":
        return messages
    sys_parts = []
    rest = []
    for m in messages:
        if m.get('role') == 'system':
            c = m.get('content')
            if c:
                sys_parts.append(c)
        else:
            rest.append(m)
    if not sys_parts:
        return messages
    res = [dict(messages[0])]
    res[0]['role'] = 'system'
    res[0]['content'] = (chr(10) + chr(10) + chr(10)).join(sys_parts)
    res.extend(rest)
    return res


_vllm_client = None


async def forward_to_vllm(vllm_body: dict, input_tokens: int, session_key: str):
    global metrics
    vllm_body["messages"] = _normalize_system_messages(vllm_body.get("messages", []))
    if not vllm_alive:
        metrics["requests_error"] += 1
        log.warning("vLLM is down - rejecting request early")
        return JSONResponse({"error": {"message": "vLLM is not available (health check failed). Start vLLM and retry."}}, status_code=503)
    try:
        client = _vllm_client
        # --- Total output budget tracking ---
        total_output_tokens = 0
        cont_count = 0
        exit_reason = "ok"
        _attempts = 0
        # --- Initial request with budget guard ---
        remaining = CTXGATE_MAX_TOTAL_OUTPUT
        _budget = _output_budget(input_tokens)
        max_tokens = min(_budget, MAX_OUTPUT, remaining)
        if max_tokens < MIN_OUTPUT:
            log.info("Non-stream: output budget %d < MIN_OUTPUT %d - stopping", max_tokens, MIN_OUTPUT)
            return JSONResponse({"error": {"message": "Output budget exhausted", "explanation": explain_status("budget")}}, status_code=413)
        vllm_body["max_tokens"] = max_tokens
        metrics["output_max_tokens_seen"] = max(metrics["output_max_tokens_seen"], max_tokens)
        while True:
            _attempts += 1
            _t0 = time.monotonic()
            resp = await client.post(VLLM_URL + "/chat/completions", json=vllm_body)
            _dt = time.monotonic() - _t0
            if _dt > 2.0:
                log.warning("vLLM pool-wait: %.1fs (possible pool saturation)", _dt)
            if resp.status_code in (500, 503) and _attempts < 3:
                _delay = 1.0 * _attempts
                log.warning("vLLM transient %d (attempt %d/3) - retrying in %.1fs", resp.status_code, _attempts, _delay)
                await asyncio.sleep(_delay)
                continue
            break
        if resp.status_code != 200:
            _et = resp.text.lower()
            _is_ctx400 = resp.status_code == 400 and any(
                p in _et for p in ("context", "max_tokens", "max_completion_tokens", "maximum", "greater than"))
            if _is_ctx400:
                log.warning("vLLM 400 (context/max_tokens) - re-shrinking and retrying: %s", resp.text[:200])
                reduced_limit = int(input_tokens * 0.8)
                vllm_body["messages"] = _emergency_shrink(vllm_body["messages"], reduced_limit)
                new_input_tokens = count_messages_tokens(vllm_body["messages"])
                _budget = _output_budget(new_input_tokens)
                max_tokens = min(_budget, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT)
                vllm_body["max_tokens"] = max_tokens
                input_tokens = new_input_tokens
                resp = await client.post(VLLM_URL + "/chat/completions", json=vllm_body)
                if resp.status_code != 200:
                    metrics["requests_error"] += 1
                    log.error("vLLM retry also failed %d: %s", resp.status_code, resp.text[:500])
                    _record_call(session_key, input_tokens, 0, f"vllm_{resp.status_code}", VLLM_MODEL, False, resp.text[:300])
                    return JSONResponse({"error": {"message": "vLLM " + str(resp.status_code), "explanation": explain_status(f"vllm_{resp.status_code}", resp.text[:300])}}, status_code=resp.status_code)
            else:
                metrics["requests_error"] += 1
                log.error("vLLM error %d: %s", resp.status_code, resp.text[:500])
                _record_call(session_key, input_tokens, 0, f"vllm_{resp.status_code}", VLLM_MODEL, False, resp.text[:300])
                return JSONResponse({"error": {"message": "vLLM " + str(resp.status_code), "explanation": explain_status(f"vllm_{resp.status_code}", resp.text[:300])}}, status_code=resp.status_code)
        data = resp.json()
        if not isinstance(data.get("usage"), dict):
            data["usage"] = {}
        choices = data.get("choices", [])
        # --- Tool call sanitization ---
        for choice in choices:
            msg = choice.get("message", {})
            if msg.get("tool_calls"):
                cleaned, stripped = sanitize_tool_calls(msg)
                if stripped:
                    metrics["toolcall_strips"] += 1
                    metrics["tool_call_suppressed"] += 1
                    choice["message"]["tool_calls"] = None
                    choice["finish_reason"] = "stop"
        output_tokens = data.get("usage", {}).get("completion_tokens", 0)
        total_output_tokens += output_tokens
        # --- Auto-continuation with total output budget ---
        ns_wall_start = time.time()
        while choices and choices[0].get("finish_reason") == "length" and cont_count < MAX_CONTINUATIONS:
            if time.time() - ns_wall_start > WALL_CLOCK_MAX:
                log.warning("Wall clock %ds exceeded - stopping non-stream", WALL_CLOCK_MAX)
                exit_reason = "wall_clock"
                break
            # Check total output budget
            remaining = CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens
            if remaining < CTXGATE_MIN_CONTINUATION_OUTPUT:
                log.info("Non-stream: total output budget exhausted (%d/%d) - stopping", total_output_tokens, CTXGATE_MAX_TOTAL_OUTPUT)
                exit_reason = "total_output_budget"
                metrics["output_total_budget_exhausted"] += 1
                break
            cont_count += 1
            metrics["output_continuations_total"] += 1
            log.info("Non-stream: auto-continuing (%d/%d, remaining=%d)", cont_count, MAX_CONTINUATIONS, remaining)
            msg_c = choices[0].get("message", {})
            # --- Continuation state machine ---
            has_tool_calls = bool(msg_c.get("tool_calls"))
            if has_tool_calls:
                # Complete tool-call set -> terminate, no continuation
                exit_reason = "tool_calls_complete"
                break
            trunc_type = _classify_truncation("length", msg_c.get("content",""), msg_c.get("reasoning_content",""), msg_c.get("tool_calls"))
            if trunc_type == "reasoning_overflow":
                cb = dict(vllm_body)
                cb["chat_template_kwargs"] = {"enable_thinking": False}
                _cb_budget = _output_budget(count_messages_tokens(vllm_body.get("messages", [])))
                cb["max_tokens"] = min(_cb_budget, MAX_OUTPUT, remaining)
                r2 = await client.post(VLLM_URL + "/chat/completions", json=cb)
                if r2.status_code == 200:
                    d2 = r2.json()
                    nc = d2.get("choices", [])
                    if nc and nc[0].get("message",{}).get("content"):
                        choices[0]["message"]["content"] = (msg_c.get("content","") or "") + nc[0]["message"]["content"]
                        choices[0]["finish_reason"] = nc[0].get("finish_reason", "stop")
                        _r2_tokens = d2.get("usage",{}).get("completion_tokens",0)
                        output_tokens += _r2_tokens
                        total_output_tokens += _r2_tokens
                exit_reason = "reasoning_overflow"
                break
            # Pure text continuation
            partial = choices[0].get("message", {}).get("content") or ""
            cont_msgs = list(vllm_body.get("messages", []))
            cont_msgs.append({"role": "assistant", "content": partial})
            cont_msgs.append({"role": "user", "content": "Continue from exactly where you left off. Do not repeat any content already provided. Resume the next word/sentence/code line."})
            cont_tokens = count_messages_tokens(cont_msgs)
            _cont_budget = _output_budget(cont_tokens)
            max_tokens = min(_cont_budget, MAX_OUTPUT, remaining)
            if cont_tokens > MAX_INPUT or max_tokens < CTXGATE_MIN_CONTINUATION_OUTPUT:
                log.info("Non-stream cont: would exceed input budget or starve output - stopping")
                exit_reason = "continuation_budget"
                break
            cont_body = dict(vllm_body)
            cont_body["messages"] = cont_msgs
            cont_body["max_tokens"] = max_tokens
            resp = await client.post(VLLM_URL + "/chat/completions", json=cont_body)
            if resp.status_code != 200:
                exit_reason = "continuation_error"
                break
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                new_c = choices[0].get("message", {}).get("content", "")
                if new_c:
                    choices[0]["message"]["content"] = partial + new_c
                _cont_tokens = data.get("usage", {}).get("completion_tokens", 0)
                output_tokens += _cont_tokens
                total_output_tokens += _cont_tokens
        # --- Finalize: add ctxgate meta ---
        truncated = exit_reason != "ok"
        tool_calls_complete = False
        tool_calls_emitted = 0
        for choice in choices:
            if choice.get("finish_reason") == "length" and choice.get("message", {}).get("content"):
                choice["message"]["content"] = _safe_truncate(choice["message"]["content"])
                choice["finish_reason"] = "length"
            if choice.get("message", {}).get("tool_calls"):
                tool_calls_complete = True
                tool_calls_emitted = len(choice["message"]["tool_calls"])
        if tool_calls_complete:
            metrics["tool_call_complete"] += 1
        if truncated:
            metrics["output_truncated_total"] += 1
            _reason_key = exit_reason if exit_reason in metrics.get("output_truncated_by_reason", {}) else exit_reason
            if _reason_key not in metrics["output_truncated_by_reason"]:
                metrics["output_truncated_by_reason"][_reason_key] = 0
            metrics["output_truncated_by_reason"][_reason_key] += 1
        data["ctxgate"] = ctxgate_meta(
            truncated=truncated,
            reason=exit_reason,
            continuations_used=cont_count,
            total_output_tokens=total_output_tokens,
            tool_calls_complete=tool_calls_complete,
            tool_calls_emitted=tool_calls_emitted,
        )["ctxgate"]
        metrics["tokens_out_total"] += output_tokens
        metrics["requests_ok"] += 1
        _ns_cached = data.get("usage", {}).get("prompt_tokens_details", {}).get("cached_tokens", 0)
        metrics["cached_tokens_total"] += _ns_cached
        metrics["prompt_tokens_total"] += input_tokens
        _track_session_tokens(session_key, 0, output_tokens, count_req=False)
        _record_call(session_key, input_tokens, output_tokens, "ok", VLLM_MODEL, False, cached_tokens=_ns_cached)
        data["usage"]["completion_tokens"] = output_tokens
        data["usage"]["total_tokens"] = input_tokens + output_tokens
        data["usage"]["prompt_tokens"] = input_tokens
        for ch in data.get("choices", []):
            msg = ch.get("message", {})
            if "reasoning" in msg and "reasoning_content" not in msg:
                msg["reasoning_content"] = msg.pop("reasoning")
        log.info("NS-DIAG session=%s exit=%s finish=%s truncated=%s conts=%d total_out=%d tc_seen=%d tc_complete=%d tc_emitted=%d",
                 session_key, exit_reason, choices[0].get("finish_reason","?") if choices else "?",
                 truncated, cont_count, total_output_tokens, tool_calls_emitted, tool_calls_complete, tool_calls_emitted)
        return JSONResponse(data)
    except httpx.TimeoutException:
        metrics["requests_error"] += 1
        _record_call(session_key, input_tokens, 0, "timeout", VLLM_MODEL, False, "vLLM 300s timeout")
        return JSONResponse({"error": {"message": "vLLM timeout", "explanation": explain_status("timeout")}, "ctxgate": ctxgate_meta(True, "timeout", 0, 0, False, 0)["ctxgate"]}, status_code=504)
    except ContextCapacityError as e:
        metrics["requests_error"] += 1
        log.error("Context capacity error: %s", e)
        return JSONResponse({"error": {"message": str(e), "explanation": explain_status("context_capacity")}, "ctxgate": ctxgate_meta(True, "context_capacity", 0, 0, False, 0)["ctxgate"]}, status_code=413)
    except Exception as e:
        metrics["requests_error"] += 1
        log.exception("vLLM forward error: %s", e)
        _record_call(session_key, input_tokens, 0, "error", VLLM_MODEL, False, str(e)[:300])
        return JSONResponse({"error": {"message": str(e), "explanation": explain_status("error", str(e)[:300])}, "ctxgate": ctxgate_meta(True, "error", 0, 0, False, 0)["ctxgate"]}, status_code=500)
def _safe_truncate(text):
    """Truncate at a safe boundary (newline, sentence end, or space)."""
    if not text:
        return text
    nl = text.rfind(chr(10))
    if nl > len(text) * 0.8:
        return text[:nl]
    for i in range(len(text) - 1, max(len(text) - 200, 0), -1):
        if text[i] in ".!?)\"'":
            return text[:i + 1]
    sp = text.rfind(" ")
    if sp > len(text) * 0.8:
        return text[:sp]
    return text






def _sse_content(text: str, chunk_id: str = "gen") -> str:
    """One SSE line carrying a content delta."""
    return "data: " + json.dumps({
        "id": chunk_id, "object": "chat.completion.chunk", "created": 0, "model": VLLM_MODEL,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }) + "\n\n"


async def stream_to_vllm(vllm_body: dict, input_tokens: int, session_key: str):
    global metrics
    vllm_body["messages"] = _normalize_system_messages(vllm_body.get("messages", []))
    async def generate():
        global metrics
        nonlocal input_tokens
        # Content is forwarded immediately. The only text ever held back is
        # the first SEAM_WINDOW chars of a continuation segment (to trim overlap
        # with what the client already has), and it is flushed on every exit path.
        SEAM_WINDOW = 120
        total_output_tokens = 0
        total_cached_tokens = 0
        full_content = ""
        seam_hold = ""
        seam_active = False
        continuation_count = 0
        current_body = dict(vllm_body)
        finish_reason = "stop"
        exit_reason = "ok"
        stream_id = "gen"
        reasoning_tail = ""
        content_tail = ""
        reasoning_chars = 0
        loop_check_acc = 0
        loop_retries = 0
        loop_period = 0
        loop_in_reasoning = False
        loop_in_content = False
        reasoning_overflow = False
        reasoning_chars_first = 0  # O2: preserve first-attempt reasoning chars
        notice = ""
        # --- ToolCallAccumulator replaces the old seg_tool_calls_seen boolean ---
        tc_accum = ToolCallAccumulator()
        tc_emitted = False  # True once we've forwarded a complete tool-call set
        loop_intentional = False
        shrink_retried = False
        client_temperature = vllm_body.get("temperature")
        sent_temperature = vllm_body.get("temperature")

        def _seam_resolve() -> str:
            nonlocal seam_hold, seam_active
            held, seam_hold, seam_active = seam_hold, "", False
            tail = full_content[-100:]
            overlap = 0
            for j in range(min(len(tail), len(held)), 0, -1):
                if held[:j] == tail[-j:]:
                    overlap = j
                    break
            if overlap > 10:
                log.info("Seam dedup: trimmed %d overlapping chars", overlap)
                held = held[overlap:]
            return held

        def _flush_seam():
            nonlocal full_content
            if seam_hold:
                out = _seam_resolve()
                if out:
                    full_content += out
                    yield _sse_content(out, stream_id)
            seam_active = False

        def _emit_ctxgate_final(fr: str, reason: str, conts: int, out_toks: int,
                                 tc_complete: bool, tc_emitted_n: int, tc_truncated: bool = False,
                                 truncated_override: bool = False):
            """Build and yield the final ctxgate chunk + [DONE]."""
            meta = ctxgate_meta(
                truncated=truncated_override if truncated_override else (reason != "ok"),
                reason=reason,
                continuations_used=conts,
                total_output_tokens=out_toks,
                tool_calls_complete=tc_complete,
                tool_calls_emitted=tc_emitted_n,
                tool_call_truncated=tc_truncated,
            )
            if reason != "ok":
                meta["ctxgate"]["reasoning_chars"] = reasoning_chars
            final_chunk = {
                "id": stream_id, "object": "chat.completion.chunk", "created": 0,
                "model": VLLM_MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": fr}],
                "ctxgate": meta["ctxgate"],
            }
            if out_toks:
                final_chunk["usage"] = {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": out_toks,
                    "total_tokens": input_tokens + out_toks,
                    "prompt_tokens_details": {"cached_tokens": total_cached_tokens},
                }
            yield "data: " + json.dumps(final_chunk) + "\n\n"
            yield "data: [DONE]\n\n"

        try:
            client = _vllm_client
            wall_start = time.time()
            # --- Initial request with total output budget guard ---
            remaining = CTXGATE_MAX_TOTAL_OUTPUT
            _budget = _output_budget(input_tokens)
            max_tokens = min(_budget, MAX_OUTPUT, remaining)
            if max_tokens < MIN_OUTPUT:
                log.info("Stream: initial output budget %d < MIN_OUTPUT %d - stopping", max_tokens, MIN_OUTPUT)
                exit_reason = "total_output_budget"
                metrics["output_total_budget_exhausted"] += 1
                for x in _flush_seam():
                    yield x
                for _cg in _emit_ctxgate_final("length", exit_reason, 0, total_output_tokens, False, 0):
                    yield _cg
                metrics["requests_error"] += 1
                _record_call(session_key, input_tokens, 0, "budget", VLLM_MODEL, True, "initial budget exhausted")
                return
            current_body["max_tokens"] = max_tokens
            metrics["output_max_tokens_seen"] = max(metrics["output_max_tokens_seen"], max_tokens)

            while True:
                if time.time() - wall_start > WALL_CLOCK_MAX:
                    log.warning("Wall clock %ds exceeded - stopping stream", WALL_CLOCK_MAX)
                    exit_reason = "wall_clock"
                    break
                finish_reason = "stop"
                got_finish = False
                got_done = False
                seg_output_tokens = 0
                interrupted = None
                loop_intentional = False
                _check_vllm_breaker()
                try:
                    async with client.stream("POST", VLLM_URL + "/chat/completions", json=current_body) as resp:
                        if resp.status_code != 200:
                            body_bytes = await resp.aread()
                            _et = body_bytes[:500].decode("utf-8", errors="replace").lower()
                            if (resp.status_code == 400 and not shrink_retried
                                    and any(p in _et for p in ("context", "max_tokens", "max_completion_tokens", "maximum", "greater than"))):
                                shrink_retried = True
                                log.warning("Stream 400 (context/max_tokens) - re-shrinking and retrying: %s", _et[:200])
                                try:
                                    current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))
                                except ContextCapacityError as cce:
                                    for x in _flush_seam():
                                        yield x
                                    for _cg in _emit_ctxgate_final("length", "context_capacity", continuation_count, total_output_tokens, False, 0):
                                        yield _cg
                                    metrics["requests_error"] += 1
                                    _record_call(session_key, input_tokens, total_output_tokens, "context_capacity", VLLM_MODEL, True, str(cce)[:200])
                                    return
                                input_tokens = count_messages_tokens(current_body["messages"])
                                _b2 = _output_budget(input_tokens)
                                current_body["max_tokens"] = min(_b2, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens)
                                continue
                            metrics["requests_error"] += 1
                            _record_call(session_key, input_tokens, total_output_tokens, "vllm_" + str(resp.status_code), VLLM_MODEL, True, body_bytes[:300].decode("utf-8", errors="replace"))
                            for x in _flush_seam():
                                yield x
                            yield "data: " + json.dumps({"error": body_bytes[:200].decode("utf-8", errors="replace")}) + "\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                got_done = True
                                break
                            try:
                                chunk = json.loads(data_str)
                                stream_id = chunk.get("id") or stream_id
                                usage = chunk.get("usage")
                                if usage:
                                    if usage.get("completion_tokens"):
                                        seg_output_tokens = usage["completion_tokens"]
                                    total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                    usage["prompt_tokens"] = input_tokens
                                    usage["total_tokens"] = input_tokens + (usage.get("completion_tokens") or 0)
                                choices = chunk.get("choices", [])
                                if not choices:
                                    continue
                                fr = choices[0].get("finish_reason")
                                if fr:
                                    finish_reason = fr
                                    got_finish = True
                                delta = choices[0].get("delta", {})
                                reasoning_piece = delta.get("reasoning_content", "") or delta.get("reasoning", "")
                                tool_calls_piece = delta.get("tool_calls")
                                if reasoning_piece:
                                    rc = {"id": stream_id, "object": "chat.completion.chunk", "created": chunk.get("created", 0), "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"reasoning_content": reasoning_piece}, "finish_reason": None}]}
                                    yield "data: " + json.dumps(rc) + "\n\n"
                                    reasoning_chars += len(reasoning_piece)
                                    reasoning_tail = (reasoning_tail + reasoning_piece)[-LOOP_TAIL:]
                                    loop_check_acc += len(reasoning_piece)
                                # --- ToolCallAccumulator: track completeness + forward in real-time ---
                                if tool_calls_piece:
                                    tc_accum.add_delta(tool_calls_piece)
                                    tc_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": chunk.get("created", 0), "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"tool_calls": tool_calls_piece}, "finish_reason": None}]}
                                    yield "data: " + json.dumps(tc_chunk) + "\n\n"
                                content_piece = delta.get("content", "")
                                if content_piece:
                                    if seam_active:
                                        seam_hold += content_piece
                                        if len(seam_hold) < SEAM_WINDOW:
                                            continue
                                        out = _seam_resolve()
                                    else:
                                        out = content_piece
                                    if out:
                                        full_content += out
                                        content_tail = (content_tail + out)[-LOOP_TAIL:]
                                        loop_check_acc += len(out)
                                        yield _sse_content(out, stream_id)
                                if loop_check_acc >= LOOP_CHECK_EVERY:
                                    loop_check_acc = 0
                                    if not loop_period:
                                        lp = _detect_loop(reasoning_tail)
                                        if lp:
                                            loop_period = lp
                                            loop_in_reasoning = True
                                            log.warning("Loop in reasoning (period=%d chars=%d) - stopping upstream", lp, reasoning_chars)
                                            loop_intentional = True
                                            break
                                        lp = _detect_loop(content_tail)
                                        if lp:
                                            loop_period = lp
                                            loop_in_content = True
                                            log.warning("Loop in content (period=%d chars=%d) - stopping stream", lp, len(full_content))
                                            loop_intentional = True
                                            break
                                if not loop_period and reasoning_chars > MAX_REASONING_TOKENS * 3 and not full_content and not tc_accum.count():
                                    loop_in_reasoning = True
                                    reasoning_overflow = True
                                    log.warning("Reasoning budget backstop (chars=%d > %d) with no content - treating as loop", reasoning_chars, MAX_REASONING_TOKENS * 3)
                                    loop_intentional = True
                                    break
                            except (json.JSONDecodeError, ValueError):
                                yield "data: " + data_str + "\n\n"
                except httpx.TransportError as e:
                    interrupted = "%s: %s" % (type(e).__name__, str(e)[:150])
                    _vllm_breaker.record_failure()

                # Release any text still held for seam trimming
                for x in _flush_seam():
                    yield x

                if interrupted is None and not got_finish and not got_done and not loop_intentional:
                    interrupted = "upstream closed the stream early"

                total_output_tokens += seg_output_tokens
                _vllm_breaker.record_success()

                # --- Tool call completeness check ---
                tc_complete = tc_accum.is_complete() if tc_accum.count() > 0 else False
                tc_incomplete = tc_accum.count() > 0 and not tc_complete

                if interrupted and not loop_intentional:
                    log.warning("Stream interrupted after %d chars: %s", len(full_content), interrupted)
                    if tc_incomplete or not full_content or continuation_count >= MAX_CONTINUATIONS:
                        exit_reason = "interrupted"
                        finish_reason = "length"
                        break
                    finish_reason = "length"
                    await asyncio.sleep(min(2 * (continuation_count + 1), 5))

                # --- Reasoning overflow: length stop with empty content ---
                if finish_reason == "length" and not full_content and not tc_accum.count():
                    if loop_retries < LOOP_RETRIES:
                        loop_retries += 1
                        exit_reason = "reasoning_overflow"
                        metrics["summary_retry_count"] += 1
                        log.info("reasoning_overflow (length, empty content) - non-thinking retry (%d/%d)", loop_retries, LOOP_RETRIES)
                        current_body = dict(vllm_body)
                        current_body["messages"] = list(vllm_body.get("messages", []))
                        _rb = _output_budget(count_messages_tokens(vllm_body.get("messages", [])))
                        current_body["max_tokens"] = min(_rb, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens)
                        current_body["chat_template_kwargs"] = {"enable_thinking": False}
                        current_body["temperature"] = RETRY_TEMPERATURE
                        current_body["top_p"] = RETRY_TOP_P
                        current_body["presence_penalty"] = RETRY_PRESENCE_PENALTY
                        sent_temperature = RETRY_TEMPERATURE
                        # O2: keep stream_options so vLLM sends usage chunk
                        if "stream_options" not in current_body:
                            current_body["stream_options"] = {"include_usage": True}
                        reasoning_tail = ""
                        content_tail = ""
                        reasoning_chars = 0
                        loop_period = 0
                        loop_check_acc = 0
                        loop_in_reasoning = False
                        loop_in_content = False
                        reasoning_overflow = False
                        tc_accum = ToolCallAccumulator()
                        seam_active = False
                        seam_hold = ""
                        continue
                    else:
                        log.warning("reasoning_overflow but no retries left - stopping")
                        exit_reason = "reasoning_overflow"
                        finish_reason = "length"
                        break

                # --- Continuation state machine ---
                if finish_reason == "length" and continuation_count < MAX_CONTINUATIONS:
                    # Incomplete tool call -> NO continuation
                    if tc_incomplete:
                        log.warning("Incomplete tool call set - NO continuation (truncated)")
                        exit_reason = "tool_call_truncated"
                        metrics["tool_call_truncated"] += 1
                        finish_reason = "length"
                        break
                    # Complete tool call set -> terminate, no continuation
                    if tc_complete:
                        log.info("Complete tool-call set - terminating turn (no continuation)")
                        tc_emitted = True
                        metrics["tool_call_complete"] += 1
                        finish_reason = "tool_calls"
                        exit_reason = "tool_calls_complete"
                        break
                    # Pure text continuation
                    remaining = CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens
                    if remaining < CTXGATE_MIN_CONTINUATION_OUTPUT:
                        log.info("Stream: total output budget exhausted (%d/%d) - stopping", total_output_tokens, CTXGATE_MAX_TOTAL_OUTPUT)
                        exit_reason = "total_output_budget"
                        metrics["output_total_budget_exhausted"] += 1
                        finish_reason = "length"
                        break
                    continuation_count += 1
                    metrics["output_continuations_total"] += 1
                    log.info("vLLM hit max_tokens - auto-continuing (%d/%d, remaining=%d)", continuation_count, MAX_CONTINUATIONS, remaining)
                    cont_messages = list(vllm_body.get("messages", []))
                    cont_messages.append({"role": "assistant", "content": full_content})
                    cont_messages.append({"role": "user", "content": "Continue from exactly where you left off. Do not repeat any content already provided. Resume the next word/sentence/code line."})
                    cont_tokens = count_messages_tokens(cont_messages)
                    _cont_budget = _output_budget(cont_tokens)
                    max_tokens = min(_cont_budget, MAX_OUTPUT, remaining)
                    if cont_tokens > MAX_INPUT or max_tokens < CTXGATE_MIN_CONTINUATION_OUTPUT:
                        log.info("Stream cont: would exceed input budget (%d > %d) or starve output (%d < %d) - stopping", cont_tokens, MAX_INPUT, max_tokens, CTXGATE_MIN_CONTINUATION_OUTPUT)
                        exit_reason = "cont_budget"
                        finish_reason = "length"
                        break
                    current_body = dict(vllm_body)
                    current_body["messages"] = cont_messages
                    current_body["max_tokens"] = max_tokens
                    metrics["output_max_tokens_seen"] = max(metrics["output_max_tokens_seen"], max_tokens)
                    seam_active = bool(full_content)
                    seam_hold = ""
                    continue
                elif finish_reason == "length":
                    log.warning("Max continuations (%d) reached - stopping", MAX_CONTINUATIONS)
                    exit_reason = "max_continuations"
                    finish_reason = "length"
                break

            # O2: save first-attempt reasoning chars before any retry resets them
            if loop_retries == 0 and reasoning_chars > 0:
                reasoning_chars_first = reasoning_chars

            # Empty-response recovery
            if (not notice and not full_content and not tc_accum.count()
                    and finish_reason == "stop" and reasoning_chars > 500
                    and loop_retries < LOOP_RETRIES):
                log.warning(
                    "Empty content after %d reasoning chars (finish=stop) - "
                    "treating as reasoning_overflow for non-thinking retry",
                    reasoning_chars,
                )
                reasoning_overflow = True

            # Loop recovery
            if (loop_in_reasoning or reasoning_overflow) and loop_retries < LOOP_RETRIES:
                loop_retries += 1
                metrics["summary_retry_count"] += 1
                log.info("Loop in reasoning - non-thinking retry (%d/%d)", loop_retries, LOOP_RETRIES)
                current_body = dict(vllm_body)
                current_body["messages"] = list(vllm_body.get("messages", []))
                _rb2 = _output_budget(count_messages_tokens(vllm_body.get("messages", [])))
                current_body["max_tokens"] = min(_rb2, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens)
                current_body["chat_template_kwargs"] = {"enable_thinking": False}
                current_body["temperature"] = RETRY_TEMPERATURE
                current_body["top_p"] = RETRY_TOP_P
                current_body["presence_penalty"] = RETRY_PRESENCE_PENALTY
                sent_temperature = RETRY_TEMPERATURE
                # O2: keep stream_options so vLLM sends usage chunk
                if "stream_options" not in current_body:
                    current_body["stream_options"] = {"include_usage": True}
                reasoning_tail = ""
                content_tail = ""
                reasoning_chars = 0
                loop_period = 0
                loop_check_acc = 0
                loop_in_reasoning = False
                loop_in_content = False
                reasoning_overflow = False
                tc_accum = ToolCallAccumulator()
                seam_active = False
                seam_hold = ""
                wall_start = time.time()
                finish_reason = "stop"
                while True:
                    if time.time() - wall_start > WALL_CLOCK_MAX:
                        exit_reason = "wall_clock"
                        break
                    finish_reason = "stop"
                    got_finish = False
                    got_done = False
                    seg_output_tokens = 0
                    interrupted = None
                    _check_vllm_breaker()
                    try:
                        async with client.stream("POST", VLLM_URL + "/chat/completions", json=current_body) as resp:
                            if resp.status_code != 200:
                                body_bytes = await resp.aread()
                                _et = body_bytes[:500].decode("utf-8", errors="replace").lower()
                                if (resp.status_code == 400 and not shrink_retried
                                        and any(p in _et for p in ("context", "max_tokens", "max_completion_tokens", "maximum", "greater than"))):
                                    shrink_retried = True
                                    log.warning("Stream 400 retry (context/max_tokens) - re-shrinking: %s", _et[:200])
                                    try:
                                        current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))
                                    except ContextCapacityError as cce:
                                        for x in _flush_seam():
                                            yield x
                                        for _cg in _emit_ctxgate_final("length", "context_capacity", continuation_count, total_output_tokens, False, 0):
                                            yield _cg
                                        metrics["requests_error"] += 1
                                        _record_call(session_key, input_tokens, total_output_tokens, "context_capacity", VLLM_MODEL, True, str(cce)[:200])
                                        return
                                    input_tokens = count_messages_tokens(current_body["messages"])
                                    _b3 = _output_budget(input_tokens)
                                    current_body["max_tokens"] = min(_b3, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens)
                                    continue
                                metrics["requests_error"] += 1
                                _record_call(session_key, input_tokens, total_output_tokens, "vllm_" + str(resp.status_code), VLLM_MODEL, True, body_bytes[:300].decode("utf-8", errors="replace"))
                                for x in _flush_seam():
                                    yield x
                                yield "data: " + json.dumps({"error": body_bytes[:200].decode("utf-8", errors="replace")}) + "\n\n"
                                yield "data: [DONE]\n\n"
                                return
                            _aiter = resp.aiter_lines().__aiter__()
                            while True:
                                try:
                                    line = await asyncio.wait_for(anext(_aiter), timeout=SSE_HEARTBEAT_INTERVAL)
                                except asyncio.TimeoutError:
                                    yield ": hb\n\n"
                                    continue
                                except StopAsyncIteration:
                                    break
                                if not line.startswith("data: "):
                                    continue
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    got_done = True
                                    break
                                try:
                                    chunk = json.loads(data_str)
                                    stream_id = chunk.get("id") or stream_id
                                    usage = chunk.get("usage")
                                    if usage:
                                        if usage.get("completion_tokens"):
                                            seg_output_tokens = usage["completion_tokens"]
                                        total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                        usage["prompt_tokens"] = input_tokens
                                        usage["total_tokens"] = input_tokens + (usage.get("completion_tokens") or 0)
                                    choices = chunk.get("choices", [])
                                    if not choices:
                                        continue
                                    fr = choices[0].get("finish_reason")
                                    if fr:
                                        finish_reason = fr
                                        got_finish = True
                                    delta = choices[0].get("delta", {})
                                    reasoning_piece = delta.get("reasoning_content", "") or delta.get("reasoning", "")
                                    tool_calls_piece = delta.get("tool_calls")
                                    if reasoning_piece:
                                        rc = {"id": stream_id, "object": "chat.completion.chunk", "created": chunk.get("created", 0), "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"reasoning_content": reasoning_piece}, "finish_reason": None}]}
                                        yield "data: " + json.dumps(rc) + "\n\n"
                                        reasoning_chars += len(reasoning_piece)
                                        reasoning_tail = (reasoning_tail + reasoning_piece)[-LOOP_TAIL:]
                                    loop_check_acc += len(reasoning_piece)
                                    if tool_calls_piece:
                                        tc_accum.add_delta(tool_calls_piece)
                                        tc_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": chunk.get("created", 0), "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"tool_calls": tool_calls_piece}, "finish_reason": None}]}
                                        yield "data: " + json.dumps(tc_chunk) + "\n\n"
                                    content_piece = delta.get("content", "")
                                    if content_piece:
                                        full_content += content_piece
                                        content_tail = (content_tail + content_piece)[-LOOP_TAIL:]
                                        loop_check_acc += len(content_piece)
                                        yield _sse_content(content_piece, stream_id)
                                except (json.JSONDecodeError, ValueError):
                                    yield "data: " + data_str + "\n\n"
                    except httpx.TransportError as e:
                        interrupted = "%s: %s" % (type(e).__name__, str(e)[:150])
                        _vllm_breaker.record_failure()
                    for x in _flush_seam():
                        yield x
                    if interrupted is None and not got_finish and not got_done:
                        interrupted = "upstream closed the stream early"
                    total_output_tokens += seg_output_tokens
                    _vllm_breaker.record_success()
                    if loop_check_acc >= LOOP_CHECK_EVERY:
                        loop_check_acc = 0
                        if not loop_period:
                            lp = _detect_loop(content_tail)
                            if lp:
                                loop_period = lp
                                loop_in_content = True
                                log.warning("Retry looped in content (period=%d) - stopping", lp)
                                break
                        if not loop_period:
                            lp = _detect_loop(reasoning_tail)
                            if lp:
                                loop_period = lp
                                loop_in_reasoning = True
                                log.warning("Retry looped in reasoning (period=%d) - stopping", lp)
                                break
                    if not loop_period and reasoning_chars > MAX_REASONING_TOKENS * 3 and not full_content and not tc_accum.count():
                        loop_in_reasoning = True
                        reasoning_overflow = True
                        log.warning("Retry reasoning budget backstop (chars=%d > %d) - stopping", reasoning_chars, MAX_REASONING_TOKENS * 3)
                        break
                    if interrupted:
                        log.warning("Retry stream interrupted after %d chars: %s", len(full_content), interrupted)
                        exit_reason = "interrupted"
                        finish_reason = "length"
                        break
                    break
                if loop_in_content:
                    exit_reason = "loop"
                    finish_reason = "length"
                elif loop_in_reasoning or reasoning_overflow:
                    exit_reason = "reasoning_overflow"
                    finish_reason = "length"
                elif finish_reason == "length":
                    # O3: retry segment hit max_tokens without completing
                    exit_reason = "retry_length"
                else:
                    exit_reason = "loop_recovered"

            # Classify empty responses
            if exit_reason in ("ok", "loop_recovered") and not full_content and not tc_accum.count():
                exit_reason = "empty"

            # --- Phase 1b: fix exit_reason for loop/overflow recovery ---
            if loop_in_content:
                exit_reason = "content_loop"
            elif loop_in_reasoning:
                exit_reason = "reasoning_loop"
            elif exit_reason == "reasoning_overflow" and not loop_in_reasoning and not loop_in_content:
                if full_content or tc_accum.count():
                    exit_reason = "ok"
                else:
                    exit_reason = "reasoning_loop"

            # --- Final ctxgate chunk + [DONE] ---
            tc_complete = tc_accum.is_complete() if tc_accum.count() > 0 else False
            tc_truncated = tc_accum.count() > 0 and not tc_complete
            tc_emitted_n = tc_accum.count() if tc_complete else 0
            if tc_truncated:
                metrics["tool_call_truncated"] += 1
                # O4: per-session tool call truncation visibility
                _tc_names = []
                for _idx, _tc in tc_accum._calls.items():
                    _tc_names.append(_tc.get("name", "?"))
                log.warning("O4 tool_call_truncated session=%s names=%s max_tokens=%d",
                           session_key, _tc_names, current_body.get("max_tokens", "?"))
            if tc_complete:
                metrics["tool_call_complete"] += 1
            # Determine final finish_reason
            if tc_complete and finish_reason != "tool_calls":
                finish_reason = "tool_calls"
            if exit_reason != "ok" and finish_reason == "stop":
                finish_reason = "length"

            # Metrics
            if exit_reason in ("ok", "loop_recovered"):
                metrics["requests_ok"] += 1
            else:
                metrics["requests_error"] += 1
            metrics["cached_tokens_total"] += total_cached_tokens
            metrics["prompt_tokens_total"] += input_tokens
            if total_output_tokens:
                metrics["tokens_out_total"] += total_output_tokens
                _track_session_tokens(session_key, 0, total_output_tokens, count_req=False)
            if exit_reason != "ok":
                metrics["output_truncated_total"] += 1
                _rk = exit_reason
                if _rk not in metrics["output_truncated_by_reason"]:
                    metrics["output_truncated_by_reason"][_rk] = 0
                metrics["output_truncated_by_reason"][_rk] += 1

            _record_call(session_key, input_tokens, total_output_tokens,
                         "ok" if exit_reason == "ok" else "error", VLLM_MODEL, True,
                         "" if exit_reason == "ok" else "stream ended: " + exit_reason,
                         cached_tokens=total_cached_tokens)

            # One concise diagnostic line
            log.info("NS-DIAG session=%s exit=%s finish=%s truncated=%s conts=%d total_out=%d tc_seen=%d tc_complete=%d tc_emitted=%d reasoning_chars=%d reasoning_chars_first=%d",
                     session_key, exit_reason, finish_reason,
                     _stream_truncated, continuation_count, total_output_tokens,
                     tc_accum.count(), tc_complete, tc_emitted_n, reasoning_chars, reasoning_chars_first)

            # Flush any remaining seam text BEFORE the final marker
            for x in _flush_seam():
                yield x

            _stream_truncated = (exit_reason != "ok") or loop_in_content or loop_in_reasoning
            for _cg in _emit_ctxgate_final(finish_reason, exit_reason, continuation_count, total_output_tokens, tc_complete, tc_emitted_n, tc_truncated, truncated_override=_stream_truncated):
                yield _cg
        except ContextCapacityError as e:
            metrics["requests_error"] += 1
            log.error("Context capacity error in stream: %s", e)
            _record_call(session_key, input_tokens, total_output_tokens, "context_capacity", VLLM_MODEL, True, str(e)[:200])
            for x in _flush_seam():
                yield x
            for _cg in _emit_ctxgate_final("length", "context_capacity", continuation_count, total_output_tokens, False, 0):
                yield _cg
        except Exception as e:
            metrics["requests_error"] += 1
            log.exception("Stream error: %s", e)
            _record_call(session_key, input_tokens, total_output_tokens, "error", VLLM_MODEL, True, str(e)[:300])
            for x in _flush_seam():
                yield x
            for _cg in _emit_ctxgate_final("length", "error", continuation_count, total_output_tokens, False, 0):
                yield _cg
    return StreamingResponse(generate(), media_type="text/event-stream")
async def _get_goose_session_id() -> str:
    """Read the most recent active session ID from Goose sessions SQLite DB (persistent connection)."""
    global sqlite_conn
    async with sqlite_lock:
        try:
            if sqlite_conn is None:
                sqlite_conn = await aiosqlite.connect(GOOSE_SESSIONS_DB)
            cursor = await sqlite_conn.execute(
                "SELECT id FROM sessions WHERE archived_at IS NULL ORDER BY updated_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                return row[0]
        except Exception as e:
            log.debug("Goose session lookup failed: %s", e)
            try:
                if sqlite_conn:
                    await sqlite_conn.close()
                sqlite_conn = None
            except Exception:
                pass
    return "unknown"

async def _get_goose_session_info(session_id: str) -> Optional[dict]:
    """Fetch full session metadata from Goose SQLite DB by session ID.

    Reuses the persistent sqlite_conn (same as _get_goose_session_id) to
    avoid spawning a new aiosqlite thread + 3 fds per call. Access is
    serialized via sqlite_lock (aiosqlite is not safe for concurrent use).
    """
    global sqlite_conn
    async with sqlite_lock:
        try:
            if sqlite_conn is None:
                sqlite_conn = await aiosqlite.connect(GOOSE_SESSIONS_DB)
            cursor = await sqlite_conn.execute(
                "SELECT id, name, session_type, working_dir, provider_name FROM sessions WHERE id = $1",
                (session_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                return {
                    "id": row[0],
                    "name": row[1] or "",
                    "session_type": row[2] or "",
                    "working_dir": row[3] or "",
                    "provider_name": row[4] or "",
                }
        except Exception as e:
            log.debug("Goose session info lookup failed for %s: %s", session_id, e)
            try:
                if sqlite_conn:
                    await sqlite_conn.close()
                sqlite_conn = None
            except Exception:
                pass
    return None


async def _enqueue_memory_job(session_id, user_content):
    if os.environ.get("CTXGATE_MEMORY_WORKER", "1") == "0":
        return
    # --- Backpressure: LOG/report lag but NEVER drop an eligible durable event ---
    try:
        pending = await _worker_pending_count()
        if pending and pending > WORKER_BACKPRESSURE:
            metrics["memory_worker_lag"] += 1
            metrics["memory_jobs_dropped"] += 1  # observability only; job is still enqueued
            log.warning("Backpressure: %d pending memory jobs > %d - logging lag (job NOT dropped)", pending, WORKER_BACKPRESSURE)
    except Exception:
        pass
    # --- Boilerplate filter: strip <turn-context> blocks ---
    import re
    cleaned = re.sub(r'<turn-context>.*?</turn-context>', '', user_content, flags=re.DOTALL).strip()
    if not cleaned or len(cleaned) < 30:
        return
    try:
        task_uuid = await _resolve_task(session_id, create=True)
        if task_uuid is None:
            return
        # --- F13: compute the canonical envelope FIRST, then fingerprint it ---
        if len(cleaned) > 5000:
            head = cleaned[:3000]
            tail = cleaned[-1500:]
            omitted = len(cleaned) - 4500
            stored = head + "\n[..." + str(omitted) + " chars omitted...\n" + tail
        else:
            stored = cleaned
        new_fp = hashlib.sha256(stored.encode()).hexdigest()[:16]
        # --- Dedup: skip if last event has same content fingerprint ---
        last_row = await pool.fetchrow(
            'SELECT content FROM proxy.events WHERE task_id=$1 ORDER BY seq DESC, id DESC LIMIT 1',
            task_uuid)
        if last_row:
            # F13: fingerprint the stored content the same way (envelope of the stored string)
            last_content = last_row['content']
            if len(last_content) > 5000:
                l_head = last_content[:3000]
                l_tail = last_content[-1500:]
                l_omitted = len(last_content) - 4500
                last_envelope = l_head + "\n[..." + str(l_omitted) + " chars omitted...\n" + l_tail
            else:
                last_envelope = last_content
            last_fp = hashlib.sha256(last_envelope.encode()).hexdigest()[:16]
            if last_fp == new_fp:
                log.debug('Dedup: skipping duplicate enqueue for session %s', session_id)
                return
        # --- Seq fix: COALESCE(MAX(seq),-1)+1 ---
        seq_row = await pool.fetchrow(
            'SELECT COALESCE(MAX(seq),-1) + 1 AS ns FROM proxy.events WHERE task_id=$1', task_uuid)
        ns = seq_row['ns'] if seq_row else 0
        ev_id = await pool.fetchval(
            'INSERT INTO proxy.events (task_id, seq, role, content) VALUES ($1,$2,$3,$4) RETURNING id',
            task_uuid, ns, 'user', stored)
        await pool.execute(
            'INSERT INTO proxy.memory_jobs (task_id, event_id, status) VALUES ($1,$2,$3)',
            task_uuid, ev_id, 'pending')
        metrics["memory_jobs_created"] += 1
        log.info('Enqueued memory job for session %s (seq %d)', session_id, ns)
    except Exception as e:
        metrics["memory_store_failure"] += 1
        log.warning('Memory job enqueue failed %s: %s', session_id, e)
async def _resolve_task(task_ref: str, create: bool = False):
    """Resolve or create a proxy task, enriching with Goose DB session metadata.

    Uses exact columns from Goose sessions DB: id, name, session_type, working_dir, provider_name.
    This ensures each Goose session gets its own properly-named proxy task.
    """
    if not pool:
        return None
    row = await pool.fetchrow("SELECT id, name FROM proxy.tasks WHERE session_id = $1", task_ref)
    if row:
        # Update metadata if it's missing (lazy enrichment)
        if not row["name"]:
            info = await _get_goose_session_info(task_ref)
            if info:
                await pool.execute(
                    "UPDATE proxy.tasks SET name=$2, session_type=$3, working_dir=$4, provider_name=$5, updated_at=now() WHERE id=$1",
                    row["id"], info["name"], info["session_type"], info["working_dir"], info["provider_name"]
                )
            else:
                await pool.execute(
                    "UPDATE proxy.tasks SET name=$2, updated_at=now() WHERE id=$1",
                    row["id"], task_ref
                )
        return str(row["id"])
    if not create:
        return None
    # Fetch Goose DB metadata for enrichment
    info = await _get_goose_session_info(task_ref)
    name = info["name"] if info else task_ref
    session_type = info["session_type"] if info else ""
    working_dir = info["working_dir"] if info else ""
    provider_name = info["provider_name"] if info else ""
    await pool.execute(
        "INSERT INTO proxy.tasks (session_id, name, session_type, working_dir, provider_name) VALUES ($1,$2,$3,$4,$5) "
        "ON CONFLICT (session_id) DO UPDATE SET name=COALESCE(EXCLUDED.name, proxy.tasks.name), "
        "session_type=COALESCE(EXCLUDED.session_type, proxy.tasks.session_type), "
        "working_dir=COALESCE(EXCLUDED.working_dir, proxy.tasks.working_dir), "
        "provider_name=COALESCE(EXCLUDED.provider_name, proxy.tasks.provider_name), updated_at=now()",
        task_ref, name, session_type, working_dir, provider_name,
    )
    row = await pool.fetchrow("SELECT id FROM proxy.tasks WHERE session_id = $1", task_ref)
    return str(row["id"])

# --- Knowledge sharing API ---
@app.post("/knowledge")
async def knowledge_create(request: Request):
    """Create or update a knowledge item. Cross-session, global."""
    if not pool:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    body = await request.json()
    domain = body.get("domain", "general")
    key = body.get("key", "")
    value = body.get("value", "")
    importance = body.get("importance", 5)
    source_session = body.get("source_session", "")
    if not key or not value:
        return JSONResponse({"error": "key and value required"}, status_code=400)
    await pool.execute(
        "INSERT INTO proxy.knowledge (domain, key, value, importance, source_session, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, now()) "
        "ON CONFLICT (domain, key) WHERE active = true "
        "DO UPDATE SET value = EXCLUDED.value, importance = GREATEST(EXCLUDED.importance, proxy.knowledge.importance), "
        "source_session = EXCLUDED.source_session, updated_at = now()",
        domain, key, value, importance, source_session
    )
    return {"status": "ok", "domain": domain, "key": key}

@app.get("/knowledge/search")
async def knowledge_search(q: str = "", domain: str = "", limit: int = 20):
    """Search knowledge items by keyword and/or domain."""
    if not pool:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    query_terms = q.split() if q else []
    rows = []
    try:
        if domain and query_terms:
            rows = await pool.fetch(
                "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                "FROM proxy.knowledge WHERE active = true AND domain = $1 "
                "AND (key ILIKE ANY($2) OR value ILIKE ANY($2)) "
                "ORDER BY importance DESC, updated_at DESC LIMIT $3",
                domain, [f"%{t}%" for t in query_terms], limit
            )
        elif domain:
            rows = await pool.fetch(
                "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                "FROM proxy.knowledge WHERE active = true AND domain = $1 "
                "ORDER BY importance DESC, updated_at DESC LIMIT $2",
                domain, limit
            )
        elif query_terms:
            rows = await pool.fetch(
                "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                "FROM proxy.knowledge WHERE active = true "
                "AND (key ILIKE ANY($1) OR value ILIKE ANY($1)) "
                "ORDER BY importance DESC, updated_at DESC LIMIT $2",
                [f"%{t}%" for t in query_terms], limit
            )
        else:
            rows = await pool.fetch(
                "SELECT key, value, importance, domain, source_session, created_at, updated_at "
                "FROM proxy.knowledge WHERE active = true "
                "ORDER BY importance DESC, updated_at DESC LIMIT $1", limit
            )
    except Exception as e:
        return {"count": 0, "items": [], "error": str(e)}
    return {
        "count": len(rows),
        "items": [
            {"key": r["key"], "value": r["value"], "importance": r["importance"],
             "domain": r["domain"], "source_session": r["source_session"] or "",
             "created_at": str(r["created_at"]), "updated_at": str(r["updated_at"])}
            for r in rows
        ]
    }

@app.get("/knowledge/stats")
async def knowledge_stats():
    """Knowledge store statistics."""
    if not pool:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    total = await pool.fetchval("SELECT COUNT(*) FROM proxy.knowledge WHERE active = true")
    by_domain = await pool.fetch(
        "SELECT domain, COUNT(*) as cnt FROM proxy.knowledge WHERE active = true GROUP BY domain ORDER BY cnt DESC"
    )
    return {"total": total, "by_domain": [{"domain": r["domain"], "count": r["cnt"]} for r in by_domain]}

@app.post("/deliverable")
async def create_deliverable(request: Request):
    """Register a new deliverable. Called by the agent when a task produces a document."""
    if not pool:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    try:
        body = await request.json()
        name = body.get("name", "unnamed")
        session_type = body.get("session_type", "")
        working_dir = body.get("working_dir", "")
        provider_name = body.get("provider_name", "")
        summary = body.get("summary", "")
        document_path = body.get("document_path", "")

        # Auto-enrichment: fill missing fields from Goose sessions DB
        if not session_type or not working_dir or not provider_name:
            x_sid = request.headers.get("X-Session-ID", "")
            if not x_sid:
                x_sid = await _get_goose_session_id()
            if x_sid and x_sid != "unknown":
                info = await _get_goose_session_info(x_sid)
                if info:
                    if not session_type:
                        session_type = info.get("session_type", "") or "goose"
                    if not working_dir:
                        working_dir = info.get("working_dir", "")
                    if not provider_name:
                        provider_name = info.get("provider_name", "")
        if not session_type:
            session_type = "goose"

        row = await pool.fetchrow(
            """INSERT INTO proxy.deliverables
               (name, session_type, working_dir, provider_name, summary, document_path)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id, name, session_type, working_dir, provider_name, summary, document_path, created_at""",
            name, session_type, working_dir, provider_name, summary, document_path
        )
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "session_type": row["session_type"],
            "working_dir": row["working_dir"],
            "provider_name": row["provider_name"],
            "summary": row["summary"],
            "document_path": row["document_path"],
            "created_at": row["created_at"].isoformat()
        }
    except Exception as e:
        log.error("deliverable create error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/deliverable")
async def list_deliverables(limit: int = 50):
    """List recent deliverables for the dashboard."""
    if not pool:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    try:
        rows = await pool.fetch(
            """SELECT id, name, session_type, working_dir, provider_name, summary, document_path, created_at, updated_at
               FROM proxy.deliverables
               ORDER BY created_at DESC
               LIMIT $1""",
            limit
        )
        return {
            "data": [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "session_type": r["session_type"],
                    "working_dir": r["working_dir"],
                    "provider_name": r["provider_name"],
                    "summary": r["summary"],
                    "document_path": r["document_path"],
                    "created_at": r["created_at"].isoformat(),
                    "updated_at": r["updated_at"].isoformat()
                }
                for r in rows
            ]
        }
    except Exception as e:
        log.error("deliverable list error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.patch("/deliverable/{id}")
async def update_deliverable(id: str, request: Request):
    """Update a deliverable's summary, name, or document_path."""
    if not pool:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    try:
        body = await request.json()
        sets = []
        params = []
        param_idx = 1
        if body.get("summary") is not None:
            sets.append("summary=$" + str(param_idx))
            params.append(body["summary"])
            param_idx += 1
        if body.get("name") is not None:
            sets.append("name=$" + str(param_idx))
            params.append(body["name"])
            param_idx += 1
        if body.get("document_path") is not None:
            sets.append("document_path=$" + str(param_idx))
            params.append(body["document_path"])
            param_idx += 1
        if not sets:
            return JSONResponse({"error": "No updatable fields provided"}, status_code=400)
        sets.append("updated_at=now()")
        params.append(id)
        row = await pool.fetchrow(
            "UPDATE proxy.deliverables SET " + ", ".join(sets) + " WHERE id=$" + str(param_idx) +
            " RETURNING id, name, session_type, working_dir, provider_name, summary, document_path, created_at, updated_at",
            *params
        )
        if not row:
            return JSONResponse({"error": "Deliverable not found"}, status_code=404)
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "session_type": row["session_type"],
            "working_dir": row["working_dir"],
            "provider_name": row["provider_name"],
            "summary": row["summary"],
            "document_path": row["document_path"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat()
        }
    except Exception as e:
        log.error("deliverable update error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
@app.post("/memory/inject")
async def memory_inject(request: Request):
    body = await request.json()
    task_ref = body.get("task_id") or body.get("task_ref")
    content = body.get("content", "")
    if not task_ref:
        return JSONResponse({"error": "task_id required"}, status_code=400)
    task_uuid = await _resolve_task(task_ref, create=True)
    if task_uuid is None:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    await pool.execute(
        "INSERT INTO proxy.working_memory (task_id, content, updated_at) VALUES ($1, $2, now()) "
        "ON CONFLICT (task_id) DO UPDATE SET content = $2, updated_at = now()",
        task_uuid, content,
    )
    log.info("Injected working memory for task %s (%d chars)", task_ref, len(content))
    return {"status": "ok", "task_id": task_ref, "task_uuid": task_uuid}

@app.get("/memory/{task_ref}")
async def memory_query(task_ref: str):
    task_uuid = await _resolve_task(task_ref, create=False)
    if task_uuid is None:
        return {"content": "", "updated_at": None}
    row = await pool.fetchrow(
        "SELECT content, updated_at FROM proxy.working_memory WHERE task_id = $1",
        task_uuid,
    )
    if not row:
        return {"content": "", "updated_at": None}
    return {"content": row["content"], "updated_at": str(row["updated_at"]) if row["updated_at"] else None}

# --- Web Dashboard ---

_lmstudio_cache = {"ts": 0.0, "data": None}

@app.get("/api/lmstudio")
async def api_lmstudio():
    """Mistral API status and stats. Cached 30s so dashboard polling
    does not trigger an outbound Mistral call on every 5s refresh."""
    global _lmstudio_cache
    _now = time.time()
    if _lmstudio_cache["data"] is not None and _now - _lmstudio_cache["ts"] < 30.0:
        return _lmstudio_cache["data"]
    result = {"available": False, "model": MISTRAL_MODEL, "engine": "Mistral API (cloud)"}
    try:
        headers = {"Authorization": f"Bearer {MISTRAL_API_KEY}"} if MISTRAL_API_KEY else {}
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://api.mistral.ai/v1/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                model_ids = [m["id"] for m in data.get("data", [])]
                result["available"] = True
                result["models_available"] = model_ids[:10]
                result["model_count"] = len(model_ids)
    except Exception as e:
        result["available"] = False
        result["error"] = f"Mistral API not reachable: {e}"
    _lmstudio_cache["ts"] = _now
    _lmstudio_cache["data"] = result
    return result


def _gpu_from_nvml() -> list:
    """Read GPU stats in-process via pynvml (zero subprocess fds)."""
    global _nvml_ready
    import pynvml
    if not _nvml_ready:
        pynvml.nvmlInitOnce()
        _nvml_ready = True
    gpus = []
    for _i in range(pynvml.nvmlDeviceGetCount()):
        _h = pynvml.nvmlDeviceGetHandleByIndex(_i)
        _name = pynvml.nvmlDeviceGetName(_h)
        _mem = pynvml.nvmlDeviceGetMemoryInfo(_h)
        _util = pynvml.nvmlDeviceGetUtilizationRate(_h)
        if isinstance(_name, bytes):
            _name = _name.decode()
        gpus.append({"name": _name, "total": str(int(_mem.total // (1024*1024))),
                     "used": str(int(_mem.used // (1024*1024))), "util": str(_util)})
    return gpus

@app.get("/api/gpu")
async def api_gpu():
    """GPU status. Prefers in-process pynvml (no fds); falls back to nvidia-smi.
    Cached 5s so health polling never spawns a process per request."""
    global _gpu_cache
    _now = time.time()
    if _gpu_cache["data"] is not None and _now - _gpu_cache["ts"] < 5.0:
        return {"gpus": _gpu_cache["data"]}
    gpus = None
    try:
        gpus = _gpu_from_nvml()
    except Exception as _e:
        log.debug("pynvml failed, falling back to nvidia-smi: %s", _e)
        gpus = None
    if gpus is None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                proc.kill()
                await proc.wait()
                raise
            gpus = []
            for _line in stdout.decode().strip().split("\n"):
                if not _line.strip():
                    continue
                _parts = [p.strip() for p in _line.split(",")]
                if len(_parts) == 4:
                    gpus.append({"name": _parts[0], "total": _parts[1].replace(" MiB", ""),
                                 "used": _parts[2].replace(" MiB", ""), "util": _parts[3].replace(" %", "")})
        except Exception:
            gpus = _gpu_cache["data"] or []
    _gpu_cache["ts"] = _now
    _gpu_cache["data"] = gpus
    return {"gpus": gpus}

# --- Memory & utilization analytics API ---
@app.get("/api/memory")
async def api_memory(session: str = "", limit: int = 50):
    """Per-session durable memories + working memory."""
    if not pool or not session:
        return {"memories": [], "working_memory": "", "count": 0}
    try:
        task_uuid = await _resolve_task(session, create=False)
        if task_uuid is None:
            return {"memories": [], "working_memory": "", "count": 0}
        rows = await pool.fetch(
            "SELECT key, value, category, importance, active, status, model_name, "
            "created_at, updated_at, source_event_id FROM proxy.memories "
            "WHERE task_id = $1 ORDER BY updated_at DESC LIMIT $2",
            task_uuid, limit,
        )
        memories = [
            {
                "key": r["key"], "value": r["value"], "category": r["category"],
                "importance": r["importance"], "active": r["active"], "status": r["status"],
                "model_name": r["model_name"],
                "created_at": str(r["created_at"]) if r["created_at"] else None,
                "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
                "source_event_id": str(r["source_event_id"]) if r["source_event_id"] else None,
            }
            for r in rows
        ]
        wrow = await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id = $1", task_uuid)
        wm = (wrow["content"] if wrow and wrow["content"] else "")
        return {"memories": memories, "working_memory": wm, "count": len(memories)}
    except Exception as e:
        log.warning("api/memory failed: %s", e)
        return {"memories": [], "working_memory": "", "count": 0, "error": str(e)}


@app.get("/api/memory-analytics")
async def api_memory_analytics():
    """Full memory/knowledge/worker utilization analytics for the dashboard."""
    result = {}
    inj = injection_metrics
    total_reqs = inj.get("total_requests", 0)
    def _rate(n):
        return (n / total_reqs) if total_reqs else 0.0
    result["injection"] = {
        "task_memory_injections": inj.get("task_memory_injections", 0),
        "task_memory_tokens": inj.get("task_memory_tokens", 0),
        "knowledge_injections": inj.get("knowledge_injections", 0),
        "knowledge_tokens": inj.get("knowledge_tokens", 0),
        "working_memory_injections": inj.get("working_memory_injections", 0),
        "working_memory_tokens": inj.get("working_memory_tokens", 0),
        "total_requests": total_reqs,
        "task_memory_rate": round(_rate(inj.get("task_memory_injections", 0)), 4),
        "knowledge_rate": round(_rate(inj.get("knowledge_injections", 0)), 4),
        "wm_rate": round(_rate(inj.get("working_memory_injections", 0)), 4),
        "last_injected_task_memory": inj.get("last_injected_task_memory", ""),
        "last_injected_knowledge": inj.get("last_injected_knowledge", ""),
        "events": inj.get("events", []),
    }

    # Memories
    try:
        if pool:
            mem_total = await pool.fetchval("SELECT COUNT(*) FROM proxy.memories")
            mem_active = await pool.fetchval("SELECT COUNT(*) FROM proxy.memories WHERE active = true")
            mem_super = await pool.fetchval("SELECT COUNT(*) FROM proxy.memories WHERE active = false")
            by_cat = await pool.fetch("SELECT category, COUNT(*) c FROM proxy.memories WHERE active = true GROUP BY category ORDER BY c DESC")
            by_imp = await pool.fetch("SELECT importance, COUNT(*) c FROM proxy.memories WHERE active = true GROUP BY importance ORDER BY importance DESC")
            by_model = await pool.fetch("SELECT model_name, COUNT(*) c FROM proxy.memories GROUP BY model_name ORDER BY c DESC")
            recent = await pool.fetch("SELECT key, value, category, importance, updated_at, model_name FROM proxy.memories WHERE active = true ORDER BY updated_at DESC LIMIT 15")
            result["memories"] = {
                "total": mem_total, "active": mem_active, "superseded": mem_super,
                "by_category": {r["category"]: r["c"] for r in by_cat},
                "by_importance": {int(r["importance"]): r["c"] for r in by_imp},
                "by_model": {r["model_name"]: r["c"] for r in by_model},
                "recent": [
                    {"key": r["key"], "value": r["value"], "category": r["category"],
                     "importance": r["importance"], "model_name": r["model_name"],
                     "updated_at": str(r["updated_at"]) if r["updated_at"] else None}
                    for r in recent
                ],
            }
        else:
            result["memories"] = {"total": 0, "active": 0, "superseded": 0, "by_category": {}, "by_importance": {}, "by_model": {}, "recent": []}
    except Exception as e:
        result["memories"] = {"total": 0, "active": 0, "superseded": 0, "by_category": {}, "by_importance": {}, "by_model": {}, "recent": [], "error": str(e)}

    # Jobs
    try:
        if pool:
            j_total = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs")
            j_done = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE status = 'done'")
            j_failed = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE status = 'failed'")
            j_pending = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE status IN ('pending','running')")
            j_avg = await pool.fetchval("SELECT COALESCE(AVG(EXTRACT(epoch FROM completed_at - started_at)) * 1000, 0) FROM proxy.memory_jobs WHERE completed_at IS NOT NULL AND started_at IS NOT NULL")
            result["jobs"] = {
                "total": j_total, "done": j_done, "failed": j_failed, "pending": j_pending,
                "avg_ms": round(j_avg or 0, 1),
                "success_rate": round(j_done / j_total, 4) if j_total else 0.0,
            }
            by_hour = await pool.fetch(
                "SELECT to_char(date_trunc('hour', completed_at), 'HH24') AS hour, "
                "COUNT(*) FILTER (WHERE status = 'done') AS done, "
                "COUNT(*) FILTER (WHERE status = 'failed') AS failed "
                "FROM proxy.memory_jobs WHERE completed_at >= now() - interval '24 hours' "
                "GROUP BY 1 ORDER BY 1"
            )
            result["jobs_by_hour"] = [{"hour": r["hour"], "done": r["done"], "failed": r["failed"]} for r in by_hour]
        else:
            result["jobs"] = {"total": 0, "done": 0, "failed": 0, "pending": 0, "avg_ms": 0, "success_rate": 0.0}
            result["jobs_by_hour"] = []
    except Exception as e:
        result["jobs"] = {"total": 0, "done": 0, "failed": 0, "pending": 0, "avg_ms": 0, "success_rate": 0.0, "error": str(e)}
        result["jobs_by_hour"] = []

    # Knowledge
    try:
        if pool:
            k_total = await pool.fetchval("SELECT COUNT(*) FROM proxy.knowledge")
            k_active = await pool.fetchval("SELECT COUNT(*) FROM proxy.knowledge WHERE active = true")
            k_dom = await pool.fetch("SELECT domain, COUNT(*) c FROM proxy.knowledge WHERE active = true GROUP BY domain ORDER BY c DESC")
            k_recent = await pool.fetch("SELECT key, value, importance, domain, updated_at FROM proxy.knowledge WHERE active = true ORDER BY updated_at DESC LIMIT 10")
            result["knowledge"] = {
                "total": k_total, "active": k_active,
                "by_domain": {r["domain"]: r["c"] for r in k_dom},
                "recent": [
                    {"key": r["key"], "value": r["value"], "importance": r["importance"],
                     "domain": r["domain"], "updated_at": str(r["updated_at"]) if r["updated_at"] else None}
                    for r in k_recent
                ],
            }
        else:
            result["knowledge"] = {"total": 0, "active": 0, "by_domain": {}, "recent": []}
    except Exception as e:
        result["knowledge"] = {"total": 0, "active": 0, "by_domain": {}, "recent": [], "error": str(e)}

    # Sessions (per-session injection breakdown)
    result["sessions"] = inj.get("sessions", {})

    # Worker
    last_job = None
    jobs_today = 0
    try:
        if pool:
            lj = await pool.fetchrow("SELECT completed_at FROM proxy.memory_jobs WHERE status = 'done' AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1")
            if lj and lj["completed_at"]:
                last_job = str(lj["completed_at"])
            jobs_today = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE status = 'done' AND completed_at >= date_trunc('day', now())")
    except Exception:
        pass
    result["worker"] = {
        "model": MISTRAL_MODEL,
        "lm_url": MISTRAL_URL + "/chat/completions",
        "poll": float(os.environ.get("CTXGATE_WORKER_POLL", "2.0")),
        "max_attempts": _env_int("CTXGATE_WORKER_MAX_ATTEMPTS", 3),
        "outage_ttl": float(os.environ.get("CTXGATE_WORKER_OUTAGE_TTL", "1800")),
        "last_job_completed_at": last_job,
        "jobs_done_today": jobs_today,
    }

    # Utilization summary (human-readable)
    n = total_reqs
    tm_i = inj.get("task_memory_injections", 0)
    kn_i = inj.get("knowledge_injections", 0)
    wm_i = inj.get("working_memory_injections", 0)
    mem_total = result.get("memories", {}).get("total", 0)
    j_done = result.get("jobs", {}).get("done", 0)
    j_avg = result.get("jobs", {}).get("avg_ms", 0)
    k_total = result.get("knowledge", {}).get("total", 0)
    result["utilization_summary"] = [
        f"Task memory injected in {_pct_str(tm_i, n)} of {n} requests ({tm_i} times)",
        f"Shared knowledge injected in {_pct_str(kn_i, n)} of {n} requests ({kn_i} times)",
        f"Working memory injected in {_pct_str(wm_i, n)} of {n} requests ({wm_i} times)",
        f"4B worker produced {mem_total} memories from {j_done} jobs (avg {(j_avg / 1000):.1f} s)",
        f"Knowledge sharing: {k_total} items, used in {_pct_str(kn_i, n)} of requests",
    ]
    return result

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ctxgate-proxy Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #0f1117; --surface: #1a1d2e; --surface2: #242842;
  --text: #e4e6f0; --text2: #8b8fa3; --accent: #6c8cff;
  --green: #4ade80; --red: #f87171; --yellow: #fbbf24; --blue: #60a5fa;
  --border: #2d3154; --radius: 12px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: 'Inter', -apple-system, system-ui, sans-serif; padding: 24px; }
h1 { font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; }
h2 { font-size: 1.1rem; font-weight: 600; color: var(--text2); margin-bottom: 16px; }
.subtitle { color: var(--text2); font-size: 0.85rem; margin-bottom: 24px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; }
.card .label { font-size: 0.75rem; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.card .value { font-size: 1.8rem; font-weight: 700; }
.card .sub { font-size: 0.8rem; color: var(--text2); margin-top: 4px; }
.card.green .value { color: var(--green); }
.card.red .value { color: var(--red); }
.card.blue .value { color: var(--blue); }
.card.yellow .value { color: var(--yellow); }
.section { margin-bottom: 24px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th { text-align: left; padding: 10px 12px; color: var(--text2); font-weight: 600; border-bottom: 1px solid var(--border); font-size: 0.75rem; text-transform: uppercase; }
td { padding: 10px 12px; border-bottom: 1px solid var(--border); }
tr:hover td { background: var(--surface2); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 0.7rem; font-weight: 600; }
.badge.ok { background: rgba(74,222,128,0.15); color: var(--green); }
.badge.error { background: rgba(248,113,113,0.15); color: var(--red); }
.badge.timeout { background: rgba(251,191,36,0.15); color: var(--yellow); }
.chart-container { position: relative; height: 250px; }
.chart-sm { position: relative; height: 150px; }
.memory-item { padding: 12px; border-left: 3px solid var(--accent); margin-bottom: 8px; background: var(--surface2); border-radius: 0 var(--radius) var(--radius) 0; font-size: 0.85rem; }
.memory-item .meta { color: var(--text2); font-size: 0.75rem; margin-top: 4px; }
#status-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 20px; font-size: 0.8rem; color: var(--text2); }
#status-bar .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
.mini-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px,1fr)); gap: 12px; margin-bottom: 14px; }
.mini-card { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.mini-card .label { font-size: 0.7rem; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
.mini-card .value { font-size: 1.4rem; font-weight: 700; }
.mini-card .sub { font-size: 0.72rem; color: var(--text2); margin-top: 3px; }
.mini-card.green .value { color: var(--green); }
.mini-card.red .value { color: var(--red); }
.mini-card.blue .value { color: var(--blue); }
.mini-card.yellow .value { color: var(--yellow); }
.scrollbox { max-height: 220px; overflow-y: auto; }
.txtbox { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 0.8rem; white-space: pre-wrap; max-height: 200px; overflow-y: auto; line-height: 1.5; }
.verdict { padding: 12px 16px; border-radius: 8px; font-weight: 700; font-size: 0.95rem; margin-bottom: 14px; }
.verdict.active { background: rgba(74,222,128,0.15); color: var(--green); border: 1px solid rgba(74,222,128,0.4); }
.verdict.inactive { background: rgba(248,113,113,0.15); color: var(--red); border: 1px solid rgba(248,113,113,0.4); }
.catbar { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 0.78rem; }
.catbar .name { width: 110px; color: var(--text2); }
.catbar .bar { flex: 1; height: 8px; background: var(--surface2); border-radius: 4px; overflow: hidden; }
.catbar .fill { height: 100%; background: var(--accent); }
.catbar .cnt { width: 24px; text-align: right; color: var(--text); }
select { background: var(--surface2); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 0.85rem; margin-bottom: 12px; }
.summary-list { list-style: none; }
.summary-list li { padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 0.85rem; color: var(--text); }
.summary-list li:last-child { border-bottom: none; }
</style>
</head>
<body>
<h1>ctxgate-proxy</h1>
<p class="subtitle">Context Proxy Dashboard &mdash; Goose&rarr;vLLM with session isolation &amp; 4B memory worker</p>
<div id="status-bar"><div class="dot" id="status-dot"></div><span id="status-text">Connecting...</span></div>

<div class="grid" id="stat-cards"></div>

<div class="row">
  <div class="section card" style="padding: 20px;">
    <h2>Token Flow</h2>
    <div class="chart-container"><canvas id="tokenChart"></canvas></div>
  </div>
  <div class="section card" style="padding: 20px;">
    <h2>Recent Calls</h2>
    <div style="max-height: 250px; overflow-y: auto;">
      <table id="calls-table">
        <thead><tr><th>Time</th><th>Session</th><th>In</th><th>Out</th><th>Cached%</th><th>Status</th><th>Stream</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>
</div>

<div class="row">
  <div class="section card" style="padding: 20px;">
    <h2>Sessions</h2>
    <div style="max-height: 300px; overflow-y: auto;">
      <table id="sessions-table">
        <thead><tr><th>Session</th><th>Req</th><th>In</th><th>Out</th><th>AvgOut</th><th>MaxCtx</th><th>Last</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>
  <div class="section card" style="padding: 20px;">
    <h2>Errors</h2>
    <div style="max-height: 300px; overflow-y: auto;" id="errors-list">
      <p style="color: var(--green); font-size: 0.85rem;">No errors</p>
    </div>
  </div>
</div>

<div class="row">
  <div class="section card" style="padding: 20px;">
    <h2>4B Memory Worker</h2>
    <div id="worker-panel"></div>
  </div>
  <div class="section card" style="padding: 20px;">
    <h2>Memory Utilization</h2>
    <div id="utilization-panel"></div>
  </div>
</div>

<div class="row">
  <div class="section card" style="padding: 20px;">
    <h2>Produced Memories</h2>
    <div id="memories-panel"></div>
  </div>
  <div class="section card" style="padding: 20px;">
    <h2>Knowledge Sharing</h2>
    <div id="knowledge-panel"></div>
  </div>
</div>

<div class="row">
  <div class="section card" style="padding: 20px;">
    <h2>4B Model (LM Studio)</h2>
    <div id="lmstudio-info">
      <p style="color: var(--text2); font-size: 0.85rem;">Loading...</p>
    </div>
  </div>
  <div class="section card" style="padding: 20px;">
    <h2>GPU Status</h2>
    <div id="gpu-info">
      <p style="color: var(--text2); font-size: 0.85rem;">Loading...</p>
    </div>
  </div>
</div>

<div class="section card" style="padding: 20px;">
  <h2>Memory &amp; Tasks</h2>
  <div id="memory-list"></div>
</div>

<div class="section card" style="padding: 20px;">
  <h2>Deliverables</h2>
  <div id="deliverables-panel">
    <p style="color:var(--text2);font-size:0.85rem;">Loading...</p>
  </div>
</div>

<script>
let tokenChart = null;
const charts = {};

function setChart(id, cfg) {
  if (charts[id]) { try { charts[id].destroy(); } catch (e) {} }
  charts[id] = new Chart(document.getElementById(id), cfg);
}

function fetchJSON(url) {
  return fetch(url).then(r => r.json());
}

function fmtNum(n) {
  if (n == null) return '0';
  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n/1000).toFixed(1) + 'K';
  return String(n);
}

function pct(n, d) {
  if (!d) return '0%';
  return (100 * n / d).toFixed(1) + '%';
}

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function badge(status) {
  const cls = status === 'ok' ? 'ok' : (status && status.includes('timeout') ? 'timeout' : 'error');
  return '<span class="badge ' + cls + '">' + esc(status) + '</span>';
}

function catBadge(c) {
  if (!c) return '<span class="badge">-</span>';
  const m = { DECISION:'#60a5fa', FINDING:'#4ade80', FAILURE:'#f87171', TODO:'#fbbf24', CONSTRAINT:'#c084fc', FILE:'#22d3ee', STATE:'#8b8fa3', FACT:'#6c8cff' };
  const col = m[c.toUpperCase()] || '#8b8fa3';
  return '<span class="badge" style="background:' + col + '22;color:' + col + '">' + esc(c) + '</span>';
}

function miniCards(items) {
  return '<div class="mini-grid">' + items.map(i =>
    '<div class="mini-card ' + (i.cls||'') + '"><div class="label">' + i.label + '</div><div class="value">' + i.value + '</div>' + (i.sub ? '<div class="sub">' + i.sub + '</div>' : '') + '</div>'
  ).join('') + '</div>';
}

function barRows(obj, maxN) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return '<p style="color:var(--text2);font-size:0.8rem;">No data</p>';
  const max = Math.max.apply(null, entries.map(e => e[1])) || 1;
  return entries.slice(0, maxN || 12).map(e =>
    '<div class="catbar"><div class="name">' + esc(e[0]) + '</div><div class="bar"><div class="fill" style="width:' + (100 * e[1] / max).toFixed(0) + '%"></div></div><div class="cnt">' + e[1] + '</div></div>'
  ).join('');
}

async function refresh() {
  try {
    const [m, sessions, calls, errors, mem] = await Promise.all([
      fetchJSON('/api/metrics'),
      fetchJSON('/api/sessions'),
      fetchJSON('/api/recent-calls?n=30'),
      fetchJSON('/api/errors?n=20'),
      fetchJSON('/api/memory-summary'),
    ]);
    let analytics = null;
    try { analytics = await fetchJSON('/api/memory-analytics'); } catch (e) { analytics = null; }

    document.getElementById('status-dot').style.background = 'var(--green)';
    document.getElementById('status-text').textContent = 'Live · ' + m.uptime_human + ' uptime · ' + m.active_sessions + ' sessions';

    const _hitRate = m.cache_hit_rate != null ? (m.cache_hit_rate * 100).toFixed(1) + '%' : 'N/A';
    const _hitCls = m.cache_hit_rate != null && m.cache_hit_rate < 0.85 ? 'red' : 'green';
    const cards = [
      { label: 'Cache Hit %', value: _hitRate, sub: 'weighted (tokens)', cls: _hitCls },
      { label: 'Effective Prefill / req', value: fmtNum(m.effective_prefill_per_request), sub: 'tokens pre-filled', cls: (m.effective_prefill_per_request || 0) > 10000 ? 'red' : 'green' },
      { label: 'Prefix Invalidations', value: m.prefix_invalidations, sub: 'deliberate misses', cls: m.prefix_invalidations > 5 ? 'yellow' : '' },
      { label: 'Requests', value: m.requests_total, sub: m.requests_ok + ' ok / ' + m.requests_error + ' err', cls: 'blue' },
      { label: 'Tokens In', value: fmtNum(m.tokens_in_total), sub: 'max ctx: ' + fmtNum(m.max_context_seen), cls: '' },
      { label: 'Tokens Out', value: fmtNum(m.tokens_out_total), sub: 'avg ' + fmtNum(m.requests_total ? Math.round(m.tokens_out_total/m.requests_total) : 0) + '/req', cls: 'green' },
      { label: 'Sessions', value: sessions.length, sub: 'active tracked', cls: 'blue' },
      { label: 'Uptime', value: m.uptime_human, sub: 'since ' + m.started_human, cls: '' },
    ];
    document.getElementById('stat-cards').innerHTML = cards.map(c =>
      '<div class="card ' + c.cls + '"><div class="label">' + c.label + '</div><div class="value">' + c.value + '</div><div class="sub">' + c.sub + '</div></div>'
    ).join('');

    const labels = calls.slice(0, 10).map(c => c.ts_human);
    const inData = calls.map(c => c.in);
    const outData = calls.map(c => c.out);
    setChart('tokenChart', {
      type: 'bar',
      data: { labels: labels, datasets: [
        { label: 'Input', data: inData, backgroundColor: 'rgba(96,165,250,0.6)', borderRadius: 4 },
        { label: 'Output', data: outData, backgroundColor: 'rgba(74,222,128,0.6)', borderRadius: 4 },
      ]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#8b8fa3' } } },
        scales: { x: { ticks: { color: '#8b8fa3', font: { size: 10 } } }, y: { ticks: { color: '#8b8fa3' } } } }
    });

    document.querySelector('#calls-table tbody').innerHTML = calls.map(c =>
      '<tr><td>' + c.ts_human + '</td><td>' + esc(c.session) + '</td><td>' + fmtNum(c.in) + '</td><td>' + fmtNum(c.out) + '</td><td>' + (c.cached_pct != null ? c.cached_pct + '%' : '-') + '</td><td>' + badge(c.status) + '</td><td>' + (c.stream ? 'yes' : 'no') + '</td></tr>'
    ).join('');

    document.querySelector('#sessions-table tbody').innerHTML = sessions.slice(0, 10).map(s =>
      '<tr><td>' + esc(s.key) + '</td><td>' + esc(s.provider) + '</td><td>' + s.requests + '</td><td>' + fmtNum(s.tokens_in) + '</td><td>' + fmtNum(s.tokens_out) + '</td><td>' + fmtNum(s.max_context) + '</td></tr>'
    ).join('') || '<tr><td colspan="6" style="color:var(--text2)">No sessions yet</td></tr>';

    document.getElementById('errors-list').innerHTML = errors.length
      ? errors.map(e => '<div class="memory-item"><div>' + e.ts_human + ' &mdash; ' + esc(e.session) + ' &mdash; <span class="badge error">' + esc(e.status) + '</span></div>' + (e.explanation ? '<div class="meta" style="color:var(--red)">' + esc(e.explanation) + '</div>' : '') + '<div class="meta">in: ' + fmtNum(e.in) + ' &middot; model: ' + esc(e.model) + '</div></div>').join('')
      : '<p style="color: var(--green); font-size: 0.85rem;">No errors</p>';

    document.getElementById('memory-list').innerHTML = mem.tasks.length
      ? mem.tasks.map(t => '<div class="memory-item"><div><strong>' + esc(t.session_id) + '</strong></div><div class="meta">created: ' + esc(t.created) + ' &middot; updated: ' + esc(t.updated) + '</div></div>').join('') +
        '<p style="color:var(--text2); font-size:0.8rem; margin-top:8px;">' + mem.memory_entries + ' memory entries in DB</p>'
      : '<p style="color:var(--text2); font-size:0.85rem;">No tasks in database</p>';

    renderWorker(analytics);
    renderUtilization(analytics);
    renderMemories(analytics);
    renderKnowledge(analytics);

    try {
      const dels = await fetchJSON('/deliverable?limit=20');
      renderDeliverables(dels.data || []);
    } catch (e) {
      document.getElementById('deliverables-panel').innerHTML = '<p style="color:var(--red);font-size:0.85rem;">Failed to load deliverables</p>';
    }

    try {
      const lm = await fetchJSON('/api/lmstudio');
      if (lm.available) {
        const activeModel = lm.active_4b || lm.model;
        document.getElementById('lmstudio-info').innerHTML =
          '<div class="memory-item"><div><strong>' + esc(activeModel) + '</strong></div>' +
          '<div class="meta">Engine: ' + esc(lm.engine) + ' &middot; Port: ' + lm.port + '</div>' +
          '<div class="meta">PID: ' + (lm.pid || '?') + ' &middot; CPU: ' + (lm.cpu_pct || '?') + '% &middot; Mem: ' + (lm.mem_pct || '?') + '% (' + (lm.rss_mb || '?') + ' MB)</div>' +
          '<div class="meta">Uptime: ' + (lm.elapsed || '?') + ' &middot; Models loaded: ' + (lm.model_count || 0) + '</div>' +
          (lm.models_loaded ? '<div class="meta" style="color:var(--green)">' + lm.models_loaded.map(esc).join(', ') + '</div>' : '') +
          '</div>';
      } else {
        document.getElementById('lmstudio-info').innerHTML =
          '<div class="memory-item" style="border-left-color: var(--red)"><div><strong style="color:var(--red)">LM Studio Offline</strong></div>' +
          '<div class="meta">' + esc(lm.error || 'Not reachable') + '</div></div>';
      }
    } catch (e) {
      document.getElementById('lmstudio-info').innerHTML =
        '<div class="memory-item" style="border-left-color:var(--red)"><div><strong style="color:var(--red)">LM Studio Unreachable</strong></div></div>';
    }

    try {
      const gpu = await fetchJSON('/api/gpu');
      if (gpu.gpus && gpu.gpus.length) {
        document.getElementById('gpu-info').innerHTML = gpu.gpus.map(g =>
          '<div class="memory-item"><div><strong>' + esc(g.name) + '</strong></div>' +
          '<div class="meta">VRAM: ' + esc(g.used) + ' / ' + esc(g.total) + ' MB &middot; Util: ' + esc(g.util) + '%</div></div>'
        ).join('');
      } else {
        document.getElementById('gpu-info').innerHTML = '<p style="color:var(--text2);font-size:0.85rem;">No GPU detected</p>';
      }
    } catch (e) {
      document.getElementById('gpu-info').innerHTML =
        '<div class="memory-item" style="border-left-color:var(--yellow)"><div><strong style="color:var(--yellow)">GPU Info Unavailable</strong></div></div>';
    }

  } catch (e) {
    document.getElementById('status-dot').style.background = 'var(--red)';
    document.getElementById('status-text').textContent = 'Connection error: ' + e.message;
  }
}

function renderWorker(a) {
  const el = document.getElementById('worker-panel');
  if (!a) { el.innerHTML = '<p style="color:var(--text2);font-size:0.85rem;">Loading analytics...</p>'; return; }
  const w = a.worker || {};
  const j = a.jobs || {};
  const cards = miniCards([
    { label: 'Model', value: w.model || '-', cls: '' },
    { label: 'Jobs Done', value: j.done || 0, sub: (j.total || 0) + ' total', cls: 'green' },
    { label: 'Failed', value: j.failed || 0, sub: (j.pending || 0) + ' pending', cls: j.failed > 0 ? 'red' : '' },
    { label: 'Avg Time', value: j.avg_ms ? (j.avg_ms / 1000).toFixed(1) + 's' : '-', sub: 'per job', cls: '' },
    { label: 'Success', value: j.success_rate != null ? (j.success_rate * 100).toFixed(1) + '%' : '-', cls: j.success_rate >= 0.99 ? 'green' : 'yellow' },
    { label: 'Last Job', value: w.last_job_completed_at ? w.last_job_completed_at.slice(11, 19) : '-', sub: w.jobs_done_today + ' today', cls: '' },
  ]);
  el.innerHTML = cards +
    '<div class="memory-item" style="margin-bottom:10px"><div class="meta">LM URL: ' + esc(w.lm_url || '-') + '</div>' +
    '<div class="meta">Poll: ' + (w.poll || '-') + 's &middot; Max attempts: ' + (w.max_attempts || '-') + ' &middot; Outage TTL: ' + (w.outage_ttl || '-') + 's</div></div>' +
    '<div class="chart-sm"><canvas id="jobsChart"></canvas></div>';
  const jobs = a.jobs_by_hour || [];
  if (jobs.length) {
    setChart('jobsChart', {
      type: 'bar',
      data: { labels: jobs.map(x => x.hour), datasets: [
        { label: 'done', data: jobs.map(x => x.done), backgroundColor: 'rgba(74,222,128,0.6)', borderRadius: 3 },
        { label: 'failed', data: jobs.map(x => x.failed), backgroundColor: 'rgba(248,113,113,0.6)', borderRadius: 3 },
      ]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { title: { display: true, text: 'Jobs over last 24h', color: '#8b8fa3', font: { size: 11 } }, legend: { labels: { color: '#8b8fa3', boxWidth: 10 } } },
        scales: { x: { ticks: { color: '#8b8fa3', font: { size: 9 }, maxRotation: 60 } }, y: { ticks: { color: '#8b8fa3', stepSize: 1 } } } }
    });
  }
}

function renderUtilization(a) {
  const el = document.getElementById('utilization-panel');
  if (!a) { el.innerHTML = '<p style="color:var(--text2);font-size:0.85rem;">Loading analytics...</p>'; return; }
  const inj = a.injection || {};
  const N = inj.total_requests || 0;
  const tmR = N ? inj.task_memory_injections / N : 0;
  const knR = N ? inj.knowledge_injections / N : 0;
  const wmR = N ? inj.working_memory_injections / N : 0;
  const cards = miniCards([
    { label: 'Task Memory', value: pct(inj.task_memory_injections, N), sub: (inj.task_memory_injections || 0) + ' times / ' + (inj.task_memory_tokens || 0) + ' tok', cls: 'blue' },
    { label: 'Shared Knowledge', value: pct(inj.knowledge_injections, N), sub: (inj.knowledge_injections || 0) + ' times / ' + (inj.knowledge_tokens || 0) + ' tok', cls: 'green' },
    { label: 'Working Memory', value: pct(inj.working_memory_injections, N), sub: (inj.working_memory_injections || 0) + ' times / ' + (inj.working_memory_tokens || 0) + ' tok', cls: 'yellow' },
  ]);
  let html = cards +
    '<div class="chart-sm" style="margin-bottom:14px"><canvas id="utilChart"></canvas></div>' +
    '<div class="label" style="margin-bottom:6px">Last Injected Task Memory</div><div class="txtbox" id="last-tm"></div>' +
    '<div class="label" style="margin:12px 0 6px">Last Injected Knowledge</div><div class="txtbox" id="last-kn"></div>';
  el.innerHTML = html;
  setChart('utilChart', {
    type: 'bar',
    data: { labels: ['Task Memory', 'Shared Knowledge', 'Working Memory'], datasets: [
      { label: 'Injection rate %', data: [(tmR * 100).toFixed(1), (knR * 100).toFixed(1), (wmR * 100).toFixed(1)],
        backgroundColor: ['rgba(96,165,250,0.7)', 'rgba(74,222,128,0.7)', 'rgba(251,191,36,0.7)'], borderRadius: 4 }
    ]},
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: '#8b8fa3' } }, y: { ticks: { color: '#8b8fa3' }, max: 100 } } }
  });
  document.getElementById('last-tm').textContent = inj.last_injected_task_memory || '(none yet)';
  document.getElementById('last-kn').textContent = inj.last_injected_knowledge || '(none yet)';
}

function renderMemories(a) {
  const el = document.getElementById('memories-panel');
  if (!a) { el.innerHTML = '<p style="color:var(--text2);font-size:0.85rem;">Loading analytics...</p>'; return; }
  const mem = a.memories || {};
  const recent = mem.recent || [];
  const sel = document.createElement('select');
  sel.id = 'mem-session';
  sel.innerHTML = '<option value="">All sessions</option>' + Object.keys(a.sessions || {}).map(s => '<option value="' + esc(s) + '">' + esc(s) + '</option>').join('');
  el.innerHTML =
    '<div class="mini-grid" style="margin-bottom:12px">' +
      '<div class="mini-card blue"><div class="label">Total</div><div class="value">' + (mem.total || 0) + '</div></div>' +
      '<div class="mini-card green"><div class="label">Active</div><div class="value">' + (mem.active || 0) + '</div></div>' +
      '<div class="mini-card red"><div class="label">Superseded</div><div class="value">' + (mem.superseded || 0) + '</div></div>' +
    '</div>' +
    '<div class="label" style="margin-bottom:6px">Category distribution</div><div style="margin-bottom:14px" id="mem-cats">' + barRows(mem.by_category || {}) + '</div>' +
    '<div class="label" style="margin-bottom:6px">Recent active memories</div>' +
    '<div class="scrollbox" style="margin-bottom:12px"><table><thead><tr><th>Key</th><th>Category</th><th>Imp</th><th>Model</th><th>Updated</th></tr></thead><tbody id="mem-recent-body"></tbody></table></div>' +
    sel.outerHTML +
    '<div id="session-mem"></div>';
  document.getElementById('mem-recent-body').innerHTML = recent.length
    ? recent.map(r => '<tr><td>' + esc(r.key) + '</td><td>' + catBadge(r.category) + '</td><td>' + (r.importance || '-') + '</td><td>' + esc(r.model_name || '') + '</td><td>' + esc((r.updated_at || '').slice(5, 16)) + '</td></tr>').join('')
    : '<tr><td colspan="5" style="color:var(--text2)">No active memories</td></tr>';
  sel.onchange = () => loadSessionMemory(sel.value);
}

function renderKnowledge(a) {
  const el = document.getElementById('knowledge-panel');
  if (!a) { el.innerHTML = '<p style="color:var(--text2);font-size:0.85rem;">Loading analytics...</p>'; return; }
  const k = a.knowledge || {};
  const inj = a.injection || {};
  const N = inj.total_requests || 0;
  const rate = N ? (inj.knowledge_injections || 0) / N : 0;
  const active = rate > 0;
  const recent = k.recent || [];
  el.innerHTML =
    '<div class="verdict ' + (active ? 'active' : 'inactive') + '">' +
      (active ? 'KNOWLEDGE SHARING ACTIVE' : 'NOT BEING UTILIZED') +
      ' &mdash; injected in ' + pct(inj.knowledge_injections, N) + ' of ' + N + ' requests</div>' +
    '<div class="mini-grid" style="margin-bottom:12px">' +
      '<div class="mini-card blue"><div class="label">Total</div><div class="value">' + (k.total || 0) + '</div></div>' +
      '<div class="mini-card green"><div class="label">Active</div><div class="value">' + (k.active || 0) + '</div></div>' +
    '</div>' +
    '<div class="label" style="margin-bottom:6px">Domain distribution</div><div style="margin-bottom:14px" id="kn-domains">' + barRows(k.by_domain || {}) + '</div>' +
    '<div class="label" style="margin-bottom:6px">Recent items</div>' +
    '<div class="scrollbox">' + (recent.length
      ? recent.map(r => '<div class="memory-item"><div><strong>' + esc(r.key) + '</strong></div><div class="meta">' + esc(r.value || '') + ' &middot; imp ' + (r.importance || '-') + ' &middot; ' + esc(r.domain || '') + '</div></div>').join('')
      : '<p style="color:var(--text2);font-size:0.85rem;">No knowledge items</p>') + '</div>';
}

function renderDeliverables(items) {
  const el = document.getElementById('deliverables-panel');
  if (!items || !items.length) {
    el.innerHTML = '<p style="color:var(--text2);font-size:0.85rem;">No deliverables yet</p>';
    return;
  }
  el.innerHTML =
    '<div class="scrollbox"><table><thead><tr><th>Name</th><th>Type</th><th>Working Dir</th><th>Provider</th><th>Summary</th><th>Document</th><th>Created</th></tr></thead><tbody>' +
    items.map(d => {
      const summary = d.summary ? (d.summary.length > 80 ? d.summary.slice(0, 77) + '...' : d.summary) : '-';
      let docLink = '-';
      if (d.document_path) {
        if (d.document_path.startsWith('http://') || d.document_path.startsWith('https://')) {
          docLink = '<a href="' + esc(d.document_path) + '" target="_blank" style="color:var(--blue);text-decoration:underline;">' + esc(d.document_path.split('/').pop()) + '</a>';
        } else {
          docLink = '<a href="file:///' + esc(d.document_path) + '" target="_blank" style="color:var(--blue);text-decoration:underline;">' + esc(d.document_path.split('/').pop()) + '</a>';
        }
      }
      const created = esc(d.created_at || '');
      const updated = d.updated_at ? ' &middot; updated ' + esc(d.updated_at) : '';
      return '<tr><td>' + esc(d.name) + '</td><td>' + esc(d.session_type) + '</td><td>' + esc(d.working_dir) + '</td><td>' + esc(d.provider_name) + '</td><td>' + esc(summary) + '</td><td>' + docLink + '</td><td>' + created + updated + '</td></tr>';
    }).join('') +
    '</tbody></table></div>';
}

async function loadSessionMemory(sid) {
  const el = document.getElementById('session-mem');
  if (!el) return;
  if (!sid) { el.innerHTML = ''; return; }
  el.innerHTML = '<p style="color:var(--text2);font-size:0.8rem;margin-top:10px">Loading ' + esc(sid) + '...</p>';
  try {
    const d = await fetchJSON('/api/memory?session=' + encodeURIComponent(sid));
    const ms = d.memories || [];
    let html = '<div class="label" style="margin:10px 0 6px">Memories for ' + esc(sid) + ' (' + (d.count || 0) + ')</div>';
    if (ms.length) {
      html += '<div class="scrollbox" style="max-height:180px"><table><thead><tr><th>Key</th><th>Category</th><th>Imp</th><th>Status</th></tr></thead><tbody>';
      html += ms.map(r => '<tr><td>' + esc(r.key) + '</td><td>' + catBadge(r.category) + '</td><td>' + (r.importance || '-') + '</td><td>' + esc(r.status || (r.active ? 'active' : 'inactive')) + '</td></tr>').join('');
      html += '</tbody></table></div>';
    } else {
      html += '<p style="color:var(--text2);font-size:0.8rem">No memories for this session</p>';
    }
    if (d.working_memory) {
      html += '<div class="label" style="margin:10px 0 6px">Working Memory</div><div class="txtbox">' + esc(d.working_memory) + '</div>';
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<p style="color:var(--red);font-size:0.8rem">Failed to load: ' + esc(e.message) + '</p>';
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    import socket as _sock
    import threading as _threading

    import uvicorn

    # Port-lock guard: probe with SO_REUSEADDR (allows binding over TIME_WAIT).
    # If a LIVE listener holds the port, wait up to 15s for it to release
    # (covers the post-SIGABRT socket teardown window). If still held, exit(1)
    # so systemd Restart=on-failure schedules a retry. NEVER exit(0) — with
    # Type=notify, exit(0) before READY=1 is a "protocol" failure that
    # on-failure does NOT restart.
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

    # limit_concurrency caps concurrent request handlers so a burst cannot open
    # unbounded request sockets (fd source). timeout_keep_alive=65 closes idle
    # keep-alive sockets before a typical client's ~75s, releasing them proactively.
    def _global_asyncio_exception_handler(loop, context):
        log.warning("Unhandled asyncio exception: %s", context.get("exception") or context)
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.set_exception_handler(_global_asyncio_exception_handler)

    config = uvicorn.Config(app, host="127.0.0.1", port=PROXY_PORT, log_level="info", timeout_graceful_shutdown=3, limit_concurrency=100, timeout_keep_alive=65)
    server = uvicorn.Server(config)
    # We manage signals ourselves (the sigwaitinfo thread below) so an external
    # SIGTERM names its sender and drains in-flight streams instead of hard-killing.
    # Disable uvicorn's own signal handling so it never installs a competing
    # handler over our blocked-signal mask:
    #   * install_signal_handlers -- the older-uvicorn hook (no-op in 0.52.x).
    #   * capture_signals -- the ACTIVE mechanism in uvicorn 0.52.x: serve() runs
    #     `with self.capture_signals():` and, in the main thread, calls
    #     signal.signal(sig, self.handle_exit) for SIGINT/SIGTERM. Left in place,
    #     uvicorn would own these signals and bypass our watcher, so we replace it
    #     with a no-op context manager.
    server.install_signal_handlers = lambda: None
    import contextlib as _contextlib
    @ _contextlib.contextmanager
    def _noop_capture_signals():
        yield
    server.capture_signals = _noop_capture_signals

    # Block SIGTERM/SIGINT/SIGHUP in the main thread BEFORE starting any other
    # threads; every thread created afterwards inherits this mask, so no thread can
    # be interrupted by these signals. A dedicated daemon thread then consumes them
    # via sigwaitinfo() (which also reports the SENDING pid, unlike a plain handler
    # which only sees our own parent). This lets us name the actual killer.
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
