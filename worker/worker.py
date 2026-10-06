"""local-llm-ctxgate-proxy 4B Memory Worker (asynchronous durable-memory extraction).

Polls proxy.memory_jobs (FOR UPDATE SKIP LOCKED, N concurrent consumers), calls the
Qwen3-4B LM Studio model with a COMPACT payload and a STRICT JSON schema
(sections 15/16), validates the response, applies deterministic dedupe /
UPDATE / SUPERSEDE to proxy.memories, updates proxy.working_memory, and marks
the job done. Qwen (27B) never waits for this worker; it is a pure async
enhancement. A bad job never kills the worker.

The 4B's own output NEVER enqueues a new memory job (no loop) - only original
Goose/user/tool events (enqueued by proxy/app.py) create jobs.

Outage handling: if LM Studio is unreachable, jobs stay pending with
exponential backoff for up to CTXGATE_WORKER_OUTAGE_TTL (default 1800s = 30 min).
Only after that window expires are jobs marked failed.
"""
import asyncio
import fcntl
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from typing import Any, Optional

import asyncpg
import httpx


class RateLimitError(Exception):
    """Mistral API rate limit (HTTP 429)."""
    pass

# --- Configuration ---
LM_URL = os.environ.get("CTXGATE_WORKER_LM_URL", os.environ.get("CTXGATE_LM_URL", "https://api.mistral.ai/v1/chat/completions"))
LM_MODEL = os.environ.get("CTXGATE_WORKER_LM_MODEL", os.environ.get("CTXGATE_LM_MODEL", "mistral-small-latest"))
LM_API_KEY = os.environ.get("CTXGATE_LM_API_KEY", "")
DSN = os.environ.get("CTXGATE_DB_DSN") or os.environ.get("CTXPROXY_DB_DSN") or "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy"
POLL = float(os.environ.get("CTXGATE_WORKER_POLL", "2.0"))
MAX_ATTEMPTS = int(os.environ.get("CTXGATE_WORKER_MAX_ATTEMPTS", "3"))
# Outage TTL: how long to keep retrying before marking jobs failed (seconds)
OUTAGE_TTL = float(os.environ.get("CTXGATE_WORKER_OUTAGE_TTL", "1800"))
# Number of concurrent consumers (parallelism). Each consumer claims one job at a time.
CONSUMERS = int(os.environ.get("CTXGATE_WORKER_CONSUMERS", "10"))
# No BASE_URL needed: Mistral is a cloud API (no local model management)
# Section 18 generation settings (established for this 4B deployment)
TEMP = 0.7
TOP_P = 0.9
MAX_TOKENS = int(os.environ.get("CTXGATE_WORKER_MAX_TOKENS", "2048"))
# Quality-check settings (self-review loop)
QC_TEMP = 0.1
QC_MAX_TOKENS = 256
QC_MAX_RETRIES = 1  # 1 retry after initial generation; if bad again -> discard
# Compact payload budget (section 8: ~500-2000 input tokens)
EVENT_EXCERPT_CHARS = 2500
WM_EXCERPT_CHARS = 1500
# TTL: days before a non-critical, never-reused memory is pruned
MEMORY_TTL_DAYS = int(os.environ.get("CTXGATE_MEMORY_TTL_DAYS", "90"))

# --- Single-instance guard (flock + heartbeat + stale takeover) ---
# Guarantees at most ONE worker polls memory_jobs. The kernel releases the
# flock automatically when the process dies (handles "dead/inactive"); the
# heartbeat + stale-kill handles a "frozen" (alive but not progressing) holder
# so a replacement can take over. See acquire_single_instance_lock().
_HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.environ.get("CTXGATE_WORKER_LOCK", os.path.join(_HERE, ".worker.lock"))
LOCK_TTL = float(os.environ.get("CTXGATE_WORKER_LOCK_TTL", "30"))  # heartbeat staleness threshold (s)
# --- Worker status file (lag + heartbeat) read by proxy + dashboard ---
STATUS_FILE = os.environ.get("CTXGATE_WORKER_STATUS", os.path.join(_HERE, ".worker_status.json"))

# --- Section 15: LM Studio SYSTEM PROMPT (configured once; sent as system role) ---
SYSTEM_PROMPT = (
    "You are the durable-memory worker for a long-running software engineering agent.\n\n"
    "Your job is to extract and maintain information that the main 27B agent will need "
    "after the current conversation context is trimmed away by the rolling window.\n\n"
    "Read the current task state and the new event. Extract ALL of the following when present: "
    "confirmed decisions, important findings, failed approaches, constraints, important TODOs, "
    "file paths being worked on, state changes, config values, architecture facts, and stable facts. "
    "ALWAYS extract the current state and subtask. If the event mentions a file, tool, or command, "
    "record it as a FILE or FACT memory. If the event shows progress on a task, record it as STATE.\n\n"
    "Rules: Do not solve the task. Do not execute tools. Do not invent information not in the input. "
    "Do not repeat information already in the working memory unless corrected. "
    "When a new fact contradicts an existing memory, use SUPERSEDE. "
    "Prefer MORE extraction over too little - the agent loses context and needs recovery data. "
    "Only return empty memory_actions if the event is truly trivial (e.g. a greeting with no content).\n\n"
    "Return only the JSON structure defined by the configured output schema. No Markdown. No "
    "commentary. No explanation outside the JSON."
)

# --- Section 16: LM Studio JSON OUTPUT SCHEMA (configured; also enforced via response_format) ---
MEMORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["memory_actions", "state_update"],
    "properties": {
        "memory_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "type", "importance", "title", "content", "source_event_id"],
                "properties": {
                    "action": {"type": "string", "enum": ["NEW", "UPDATE", "SUPERSEDE", "DUPLICATE", "NO_CHANGE"]},
                    "type": {"type": "string", "enum": ["DECISION", "FINDING", "FAILURE", "TODO", "CONSTRAINT", "FILE", "STATE", "FACT"]},
                    "importance": {"type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"]},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "source_event_id": {"type": "string"},
                },
            },
        },
        "state_update": {
            "type": "object",
            "additionalProperties": False,
            "required": ["changed", "current_state", "current_subtask"],
            "properties": {
                "changed": {"type": "boolean"},
                "current_state": {"type": ["string", "null"]},
                "current_subtask": {"type": ["string", "null"]},
            },
        },
    },
}

# Section 11 importance -> int (keeps existing 1-10 column + importance DESC ordering)
IMP_MAP = {"CRITICAL": 10, "HIGH": 7, "NORMAL": 5, "LOW": 2}
VALID_ACTIONS = set(MEMORY_SCHEMA["properties"]["memory_actions"]["items"]["properties"]["action"]["enum"])
VALID_TYPES = set(MEMORY_SCHEMA["properties"]["memory_actions"]["items"]["properties"]["type"]["enum"])
VALID_IMP = set(MEMORY_SCHEMA["properties"]["memory_actions"]["items"]["properties"]["importance"]["enum"])

log = logging.getLogger("w")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

pool: Optional[asyncpg.Pool] = None
client: Optional[httpx.AsyncClient] = None
lock_fd: Optional[int] = None
running = True

# Outage tracking
outage_since: Optional[float] = None  # timestamp when outage started
model_loaded: bool = False
consecutive_lm_failures: int = 0
_last_status_write = 0.0  # throttle timestamp for status-file writes
# Lag / completion tracking (source of the worker_lag_seconds metric)
last_completion: Optional[float] = None  # time.time() of the last successful job
jobs_done_total: int = 0
pending_jobs_cache: int = 0  # cached count of pending jobs (updated by poll loop)
# Throttling / context tracking
lm_requests: list = []  # timestamps of completed LM calls (for RPM)
lm_context_tokens: list = []  # context token counts per request
lm_total_tokens_in: int = 0  # cumulative input tokens
lm_total_tokens_out: int = 0  # cumulative output tokens
lm_last_latency_ms: float = 0.0  # most recent call latency

def _sig(s, f):
    global running
    running = False

def _norm(s: str) -> str:
    """Normalize a title/key for deterministic matching (case/whitespace/punct)."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", " ", s)

# --- Structural validation (section 16: validate before touching PostgreSQL) ---
def validate_response(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if "memory_actions" not in obj or "state_update" not in obj:
        return False
    acts = obj["memory_actions"]
    if not isinstance(acts, list):
        return False
    for a in acts:
        if not isinstance(a, dict):
            return False
        if a.get("action") not in VALID_ACTIONS:
            return False
        if a.get("type") not in VALID_TYPES:
            return False
        if a.get("importance") not in VALID_IMP:
            return False
        if not isinstance(a.get("title"), str) or not isinstance(a.get("content"), str):
            return False
        if "source_event_id" not in a:
            return False
    su = obj["state_update"]
    if not isinstance(su, dict):
        return False
    if "changed" not in su or "current_state" not in su or "current_subtask" not in su:
        return False
    if not isinstance(su["changed"], bool):
        return False
    return True

# --- Compact payload builder (section 8) ---
def build_payload(task_desc: str, wm: str, event: dict) -> str:
    """Build a compact LM payload from a task, working memory, and event.

    Handles role in (user, assistant, tool) so the worker can extract
    durable facts from ALL meaningful session events, not just user messages.
    Tool-result events carry findings, file paths, decisions, failures, and
    state changes that would otherwise be lost.
    """
    role = event.get("role", "user")
    content = (event.get("content") or "")[:EVENT_EXCERPT_CHARS]
    tool = event.get("tool_calls")
    tool_txt = ""
    if tool:
        try:
            tool_txt = json.dumps(tool)[:600]
        except Exception:
            tool_txt = str(tool)[:600]
    changed = event.get("changed_files") or ""

    # Role-specific framing so the 4B knows what kind of event it is looking at
    if role == "assistant":
        event_label = "ASSISTANT MESSAGE (decisions, findings, state changes, plan updates)"
    elif role == "tool":
        event_label = "TOOL RESULT (findings, file paths, errors, state changes, command output)"
    else:
        event_label = "USER MESSAGE"

    parts = [
        "CURRENT TASK: " + (task_desc or "(none)"),
        "CURRENT WORKING MEMORY: " + (wm or "(empty)")[:WM_EXCERPT_CHARS],
        "NEW EVENT (" + event_label + "): " + content,
    ]
    if tool_txt:
        parts.append("TOOL CALLS/RESULT EXCERPT: " + tool_txt)
    if changed:
        parts.append("CHANGED FILES: " + changed)
    parts.append("source_event_id: " + str(event.get("id", "")))
    return "\n".join(parts)

# --- Pre-load: ensure model is loaded before first completion ---
async def ensure_model_loaded() -> bool:
    """Check Mistral API connectivity. Cloud API - no model loading needed.
    
    Returns True if the API is reachable, False if unreachable.
    """
    global model_loaded
    if model_loaded:
        return True
    try:
        r = await client.get(LM_URL.rsplit("/chat/completions", 1)[0] + "/models", timeout=10)
        if r.status_code == 200:
            model_loaded = True
            log.info("Mistral API reachable (model=%s)", LM_MODEL)
            return True
        log.warning("Mistral API returned HTTP %d", r.status_code)
        return False
    except Exception as e:
        model_loaded = False
        log.warning("ensure_model_loaded: Mistral API unreachable: %s", e)
        return False


def _extract_json(raw: str) -> Any:
    """Extract JSON from model output that may have preamble text.
    
    Handles models that wrap JSON in markdown code fences or add
    preamble text. Tries:
    1. Direct json.loads
    2. Find first { ... last }
    3. Find first [ ... last ] (for arrays)
    """
    raw = raw.strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace:last_brace + 1]
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    first_bracket = raw.find("[")
    last_bracket = raw.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        candidate = raw[first_bracket:last_bracket + 1]
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    raise ValueError("No valid JSON found in model output: " + raw[:200])

# --- Mistral API call (json_schema enforced) ---
async def _wait_for_lm_studio(max_wait: float = 600.0):
    """No-op: Mistral is a cloud API, no local contention to wait for.
    Kept for call-site compatibility."""
    pass


async def call_4b(payload: str) -> dict:
    body = {
        "model": LM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        "temperature": TEMP,
        "top_p": TOP_P,
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "memory_worker", "strict": True, "schema": MEMORY_SCHEMA},
        },
    }
    t0 = time.time()
    r = await client.post(LM_URL, json=body, timeout=httpx.Timeout(300, connect=10))
    latency_ms = round((time.time() - t0) * 1000, 1)
    if r.status_code == 429:
        raise RateLimitError("Mistral rate limit (429): %s" % r.text[:200])
    if r.status_code != 200:
        raise RuntimeError("Mistral HTTP %d: %s" % (r.status_code, r.text[:200]))
    data = r.json()
    if "choices" not in data or not data["choices"]:
        raise RuntimeError("Mistral response missing choices: %s" % str(data)[:200])
    msg = data["choices"][0]["message"]
    content = msg.get("content", "")
    if not content:
        raise RuntimeError("Mistral returned empty content")
    # Track throttling metrics
    global lm_requests, lm_context_tokens, lm_total_tokens_in, lm_total_tokens_out, lm_last_latency_ms
    now = time.time()
    lm_requests.append(now)
    # Keep only last 5 min of requests for RPM calc
    cutoff = now - 300
    lm_requests = [t for t in lm_requests if t >= cutoff]
    lm_last_latency_ms = latency_ms
    # Extract token usage if present in response
    usage = data.get("usage", {})
    tin = usage.get("prompt_tokens", 0)
    tout = usage.get("completion_tokens", 0)
    lm_total_tokens_in += tin
    lm_total_tokens_out += tout
    lm_context_tokens.append(tin)
    # Keep only last 100 for context trend
    if len(lm_context_tokens) > 100:
        lm_context_tokens = lm_context_tokens[-100:]
    return _extract_json(content)

# --- Quality check: 4B self-reviews its own output ---
QUALITY_CHECK_PROMPT = (
    "You are a strict quality reviewer for memory entries.\n"
    "Evaluate the given memory actions for: factual accuracy, specificity, usefulness, no hallucination, no redundancy.\n"
    "If ANY entry is vague, invented, redundant, or unhelpful, mark quality as bad.\n"
    "Return only the JSON object. No commentary.\n"
)

QUALITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["quality", "reason"],
    "properties": {
        "quality": {"type": "string", "enum": ["good", "bad"]},
        "reason": {"type": "string"},
    },
}

async def call_4b_quality_check(memory_actions: list) -> dict:
    """Ask Mistral to self-review its generated memory entries."""
    payload = "Evaluate these memory entries for quality:\n" + json.dumps(memory_actions, indent=2)
    body = {
        "model": LM_MODEL,
        "messages": [
            {"role": "system", "content": QUALITY_CHECK_PROMPT},
            {"role": "user", "content": payload},
        ],
        "temperature": QC_TEMP,
        "top_p": 0.9,
        "max_tokens": QC_MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "quality_check", "strict": True, "schema": QUALITY_SCHEMA},
        },
    }
    r = await client.post(LM_URL, json=body, timeout=httpx.Timeout(300, connect=10))
    if r.status_code == 429:
        raise RateLimitError("Mistral rate limit (429) in quality check: %s" % r.text[:200])
    if r.status_code != 200:
        raise RuntimeError("Quality check HTTP %d: %s" % (r.status_code, r.text[:200]))
    msg = r.json()["choices"][0]["message"]
    content = msg.get("content", "")
    if not content:
        raise RuntimeError("Quality check: empty response")
    return _extract_json(content)

# --- Deterministic dedupe / UPDATE / SUPERSEDE (section 12) ---
async def apply_memories(task_id: str, event_id: str, resp: dict, conn=None) -> int:
    """Apply memory actions (NEW/UPDATE/SUPERSEDE) in a SINGLE transaction.

    All reads (existing memories) and writes (inserts/updates/supersedes)
    happen in one txn so concurrent consumers never create duplicate
    durable memories for the same (task_id, source_event_id, key_norm).

    If *conn* is provided (caller manages the transaction), use it directly.
    Otherwise acquire a connection and manage the transaction here.
    """
    if conn is not None:
        return await _do_apply_memories(conn, task_id, event_id, resp)
    async with pool.acquire() as c:
        async with c.transaction():
            return await _do_apply_memories(c, task_id, event_id, resp)

async def _do_apply_memories(conn, task_id: str, event_id: str, resp: dict) -> int:
    """Inner implementation of apply_memories, operating on a given connection."""
    applied = 0
    for a in resp.get("memory_actions", []):
        action = a["action"]
        if action in ("NO_CHANGE", "DUPLICATE"):
            continue
        title = (a["title"] or "").strip()
        content = (a["content"] or "").strip()
        if not title or not content:
            continue
        category = a["type"]
        imp = IMP_MAP.get(a["importance"], 5)
        mkey = _norm(title)
        # Find an existing ACTIVE memory of the same type+title for this task
        row = await conn.fetchrow(
            "SELECT id FROM proxy.memories WHERE task_id=$1 AND active=true AND category=$2 "
            "AND key_norm = $3 LIMIT 1",
            task_id, category, mkey,
        )
        if action == "SUPERSEDE":
            if row:
                nid = await conn.fetchval(
                    "INSERT INTO proxy.memories(task_id,key,value,category,importance,source_event_id,status,model_name,key_norm) "
                    "VALUES($1,$2,$3,$4,$5,$6,'active',$7,$8) RETURNING id",
                    task_id, title, content, category, imp, event_id, LM_MODEL, mkey,
                )
                await conn.execute(
                    "UPDATE proxy.memories SET active=false,status='superseded',superseded_by=$1,updated_at=now() WHERE id=$2",
                    nid, row["id"],
                )
                log.info("SUPERSEDE %s (old=%s new=%s)", title, row["id"], nid)
            else:
                await conn.execute(
                    "INSERT INTO proxy.memories(task_id,key,value,category,importance,source_event_id,status,model_name,key_norm) "
                    "VALUES($1,$2,$3,$4,$5,$6,'active',$7,$8)",
                    task_id, title, content, category, imp, event_id, LM_MODEL, mkey,
                )
                log.info("SUPERSEDE(no-old) -> INSERT %s", title)
            applied += 1
        elif action == "UPDATE":
            if row:
                await conn.execute(
                    "UPDATE proxy.memories SET value=$1,importance=$2,source_event_id=$3,updated_at=now() WHERE id=$4",
                    content, imp, event_id, row["id"],
                )
                log.info("UPDATE %s", title)
            else:
                await conn.execute(
                    "INSERT INTO proxy.memories(task_id,key,value,category,importance,source_event_id,status,model_name,key_norm) "
                    "VALUES($1,$2,$3,$4,$5,$6,'active',$7,$8)",
                    task_id, title, content, category, imp, event_id, LM_MODEL, mkey,
                )
                log.info("UPDATE(no-old) -> INSERT %s", title)
            applied += 1
        else:  # NEW
            if row:
                # Deterministic dedupe: same (type,title) already active -> refresh, don't duplicate
                await conn.execute(
                    "UPDATE proxy.memories SET value=$1,importance=$2,source_event_id=$3,updated_at=now() WHERE id=$4",
                    content, imp, event_id, row["id"],
                )
                log.info("NEW(dup) -> refresh %s", title)
            else:
                await conn.execute(
                    "INSERT INTO proxy.memories(task_id,key,value,category,importance,source_event_id,status,model_name,key_norm) "
                    "VALUES($1,$2,$3,$4,$5,$6,'active',$7,$8)",
                    task_id, title, content, category, imp, event_id, LM_MODEL, mkey,
                )
                log.info("NEW %s imp=%d", title, imp)
            applied += 1
    return applied

async def update_working_memory(task_id: str, su: dict, conn=None) -> None:
    """Refresh working memory. If *conn* is provided, use it (caller manages txn)."""
    # Refresh WM for every substantive turn - not just when 'changed' is true.
    # The 4B always returns current_state; we use it to keep WM fresh.
    state = su.get("current_state") or ""
    subtask = su.get("current_subtask") or ""
    if not state and not subtask:
        return
    content = "STATE: " + state + (" | SUBTASK: " + subtask if subtask else "")
    c = conn if conn is not None else pool
    await c.execute(
        "INSERT INTO proxy.working_memory(task_id,content,updated_at) VALUES($1,$2,now()) "
        "ON CONFLICT(task_id) DO UPDATE SET content=$2,updated_at=now()",
        task_id, content[:2000],
    )
    log.info("WM updated: %s", content[:120])
async def prune_memories(pool) -> None:
    """Slow-cycle prune: hard-expire past expires_at, age-prune stale non-critical rows.

    - Hard expire: DELETE rows where expires_at < now()
    - Age prune: DELETE active, non-critical (importance < 10) rows whose
      last_accessed_at is older than MEMORY_TTL_DAYS. Rows with NULL
      last_accessed_at are never pruned (we can't prove they're stale).
    - CRITICAL rows (importance = 10) are never pruned.
    """
    try:
        status1 = await pool.execute(
            "DELETE FROM proxy.memories WHERE expires_at IS NOT NULL AND expires_at < now()"
        )
        hard_expired = int(status1.split()[-1]) if status1 else 0
        status2 = await pool.execute(
            "DELETE FROM proxy.memories WHERE active AND importance < 10 "
            "AND last_accessed_at IS NOT NULL "
            "AND last_accessed_at < now() - make_interval(days => $1)",
            MEMORY_TTL_DAYS,
        )
        age_pruned = int(status2.split()[-1]) if status2 else 0
        log.info("memory prune: hard_expired=%d age_pruned=%d (ttl_days=%d)",
                 hard_expired, age_pruned, MEMORY_TTL_DAYS)
    except Exception as e:
        log.warning("memory prune failed (non-fatal): %s", e)

async def claim_jobs(n: int = 1) -> list:
    """Claim up to N pending jobs with row locking (FOR UPDATE SKIP LOCKED).
    With N consumers, we claim N jobs in one query so each consumer gets one.
    Sets claimed_at so stuck-job recovery can detect crashed consumers."""
    rows = await pool.fetch(
        "SELECT id, task_id, event_id, attempts FROM proxy.memory_jobs "
        "WHERE status='pending' ORDER BY created_at LIMIT $1 FOR UPDATE SKIP LOCKED",
        n
    )
    if rows:
        ids = [str(r["id"]) for r in rows]
        placeholders = ",".join("$" + str(i+1) for i in range(len(ids)))
        await pool.execute(
            "UPDATE proxy.memory_jobs SET status='processing', started_at=now(), claimed_at=now() "
            "WHERE id IN (" + placeholders + ")",
            *ids,
        )
    return list(rows)

async def recover_stuck_jobs(stale_seconds: float = 300.0) -> int:
    """Recover jobs stuck in 'processing' (worker crashed mid-claim).

    Resets jobs whose claimed_at is older than *stale_seconds* back to 'pending'.
    Jobs older than OUTAGE_TTL are marked 'failed' instead.
    Called on worker start and periodically from the poll loop.
    """
    try:
        # Mark truly stuck jobs (older than OUTAGE_TTL) as failed
        failed = await pool.execute(
            "UPDATE proxy.memory_jobs SET status='failed', completed_at=now(), "
            "error='stuck in processing beyond OUTAGE_TTL' "
            "WHERE status='processing' AND claimed_at IS NOT NULL "
            "AND claimed_at < now() - make_interval(secs => $1)",
            int(OUTAGE_TTL),
        )
        failed_n = int(failed.split()[-1]) if failed else 0
        # Reset recently-stuck jobs back to pending
        recovered = await pool.execute(
            "UPDATE proxy.memory_jobs SET status='pending', started_at=NULL, claimed_at=NULL "
            "WHERE status='processing' AND claimed_at IS NOT NULL "
            "AND claimed_at < now() - make_interval(secs => $1) "
            "AND claimed_at >= now() - make_interval(secs => $2)",
            int(stale_seconds), int(OUTAGE_TTL),
        )
        recovered_n = int(recovered.split()[-1]) if recovered else 0
        if failed_n or recovered_n:
            log.info("Stuck-job recovery: %d failed, %d reset to pending", failed_n, recovered_n)
        return failed_n + recovered_n
    except Exception as e:
        log.warning("Stuck-job recovery failed (non-fatal): %s", e)
        return 0

async def process_job(job) -> None:
    """Process a single memory job with outage-aware retry logic.
    
    On LM Studio unreachable (connection error):
    - If within OUTAGE_TTL: requeue as pending with exponential backoff
    - If past OUTAGE_TTL: mark as failed
    On other errors: use standard MAX_ATTEMPTS bounded retry
    """
    global outage_since, last_completion, jobs_done_total, consecutive_lm_failures, model_loaded
    jid, task_id, event_id = str(job["id"]), str(job["task_id"]), str(job["event_id"]) if job["event_id"] else None
    # Safety net: ensure claimed_at is set (claim_jobs already does this, but guard against edge cases)
    await pool.execute("UPDATE proxy.memory_jobs SET status='processing',started_at=now(),claimed_at=COALESCE(claimed_at,now()) WHERE id=$1", jid)
    try:
        # Load current task + working memory + event (compact)
        task_desc = ""
        trow = await pool.fetchrow("SELECT session_id, status FROM proxy.tasks WHERE id=$1", task_id)
        if trow:
            task_desc = "session=" + str(trow["session_id"])
        wm = ""
        wrow = await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_id)
        if wrow:
            wm = wrow["content"]
        event = None
        if event_id:
            erow = await pool.fetchrow(
                "SELECT id, role, content, tool_calls FROM proxy.events WHERE id=$1", event_id)
            if erow:
                event = dict(erow)
        if event is None:
            # No event to process -> nothing to extract; mark done
            await pool.execute(
                "UPDATE proxy.memory_jobs SET status='done',completed_at=now(),result='{}'::jsonb WHERE id=$1", jid)
            return
        payload = build_payload(task_desc, wm, event)
        await _wait_for_lm_studio()
        resp = await call_4b(payload)
        # Success: reset outage tracker
        outage_since = None
        consecutive_lm_failures = 0

        # --- Quality-check loop: self-review, retry once, discard if bad again ---
        acts = resp.get("memory_actions", [])
        if acts:  # only check if there are entries to review
            for qc_attempt in range(QC_MAX_RETRIES + 1):  # 0=initial check, 1=retry check
                await _wait_for_lm_studio()
                qc = await call_4b_quality_check(acts)
                if qc.get("quality") == "good":
                    log.info("Quality check PASSED (attempt %d): %s", qc_attempt + 1, qc.get("reason", ""))
                    break
                log.warning("Quality check FAILED (attempt %d/%d): %s", qc_attempt + 1, QC_MAX_RETRIES + 1, qc.get("reason", ""))
                if qc_attempt < QC_MAX_RETRIES:
                    # Regenerate
                    await _wait_for_lm_studio()
                    resp = await call_4b(payload)
                    acts = resp.get("memory_actions", [])
                else:
                    # Bad again -> discard all entries
                    log.info("Quality check failed twice -> discarding all memory entries")
                    resp = {"memory_actions": [], "state_update": resp.get("state_update", {"changed": False, "current_state": None, "current_subtask": None})}
                    acts = []

        if not validate_response(resp):
            raise ValueError("4B response failed schema validation")
        # --- Single transaction: apply memories + update WM + mark job done ---
        # This guarantees no partial state: if any step fails, the whole txn
        # rolls back and the job stays 'processing' (reclaimable by stuck-job recovery).
        async with pool.acquire() as conn:
            async with conn.transaction():
                applied = await _do_apply_memories(conn, task_id, event_id or jid, resp)
                await update_working_memory(task_id, resp.get("state_update", {}), conn=conn)
                await conn.execute(
                    "UPDATE proxy.memory_jobs SET status='done',completed_at=now(),result=$1,attempts=$2 WHERE id=$3",
                    json.dumps(resp), int(job.get("attempts") or 0) + 1, jid,
                )
        log.info("JOB done %s (applied=%d)", jid, applied)
        last_completion = time.time()
        jobs_done_total += 1
        _write_status(force=True)
    except RateLimitError as e:
        # Mistral rate limit (429) - back off and requeue (same as outage)
        now = time.time()
        if outage_since is None:
            outage_since = now
            log.warning("Mistral rate limit STARTED (job %s): %s", jid, e)
        elapsed = now - outage_since
        if elapsed < OUTAGE_TTL:
            attempts = int(job.get("attempts") or 0) + 1
            backoff = min(120, 2 ** min(attempts, 7))
            log.info("JOB %s: rate limit %.0fs/%.0fs, requeue in %ds", jid, elapsed, OUTAGE_TTL, backoff)
            await pool.execute(
                "UPDATE proxy.memory_jobs SET status='pending',attempts=$1,error=$2 WHERE id=$3",
                attempts, "rate_limit: " + str(e)[:200], jid,
            )
            await asyncio.sleep(backoff)
        else:
            attempts = int(job.get("attempts") or 0) + 1
            log.error("JOB %s: rate limit exceeded TTL %.0fs, marking failed", jid, OUTAGE_TTL)
            await pool.execute(
                "UPDATE proxy.memory_jobs SET status='failed',attempts=$1,error=$2,completed_at=now() WHERE id=$3",
                attempts, "rate limit exceeded TTL (%.0fs): %s" % (OUTAGE_TTL, str(e)[:300]), jid,
            )
            outage_since = None
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout) as e:
        # Mistral API unreachable/outage - use TTL-based backoff
        now = time.time()
        consecutive_lm_failures += 1
        if consecutive_lm_failures >= 3:
            model_loaded = False
            consecutive_lm_failures = 0
            log.warning("3 consecutive LM failures - resetting model_loaded")
        if outage_since is None:
            outage_since = now
            log.warning("Mistral API outage STARTED (job %s): %s", jid, e)
        elapsed = now - outage_since
        if elapsed < OUTAGE_TTL:
            # Keep pending with exponential backoff (capped at 60s)
            attempts = int(job.get("attempts") or 0) + 1
            backoff = min(60, 2 ** min(attempts, 6))
            log.info("JOB %s: outage %.0fs/%.0fs, requeue in %ds", jid, elapsed, OUTAGE_TTL, backoff)
            await pool.execute(
                "UPDATE proxy.memory_jobs SET status='pending',attempts=$1,error=$2 WHERE id=$3",
                attempts, "outage: " + str(e)[:200], jid,
            )
            await asyncio.sleep(backoff)
        else:
            # TTL expired - mark failed
            attempts = int(job.get("attempts") or 0) + 1
            log.error("JOB %s: outage exceeded TTL %.0fs, marking failed", jid, OUTAGE_TTL)
            await pool.execute(
                "UPDATE proxy.memory_jobs SET status='failed',attempts=$1,error=$2,completed_at=now() WHERE id=$3",
                attempts, "outage exceeded TTL (%.0fs): %s" % (OUTAGE_TTL, str(e)[:300]), jid,
            )
            outage_since = None
    except Exception as e:
        # Non-outage error: standard bounded retry.
        # Only count as an LM failure if the error originated from LM Studio itself
        # (call_4b / call_4b_quality_check raise RuntimeError on HTTP != 200 or bad payloads).
        # Our own logic errors (validate_response ValueError, DB errors) must NOT
        # reset model_loaded, because the model is fine in those cases.
        if isinstance(e, RuntimeError):
            consecutive_lm_failures += 1
            if consecutive_lm_failures >= 3:
                model_loaded = False
                consecutive_lm_failures = 0
                log.warning("3 consecutive LM failures - resetting model_loaded")
        attempts = int(job.get("attempts") or 0) + 1
        log.warning("JOB %s failed (attempt %d/%d): %s", jid, attempts, MAX_ATTEMPTS, e)
        if attempts >= MAX_ATTEMPTS:
            await pool.execute(
                "UPDATE proxy.memory_jobs SET status='failed',attempts=$1,error=$2,completed_at=now() WHERE id=$3",
                attempts, str(e)[:500], jid,
            )
        else:
            # Requeue for a bounded retry
            await pool.execute(
                "UPDATE proxy.memory_jobs SET status='pending',attempts=$1,error=$2 WHERE id=$3",
                attempts, str(e)[:500], jid,
            )

# Track active consumer tasks for the status file
_active_consumers: dict = {}  # consumer_id -> asyncio.Task

async def _consumer(cid: int) -> None:
    """One consumer loop: claim one job at a time, process it, repeat."""
    global model_loaded
    while running:
        try:
            job = await claim_jobs(1)
            if job:
                if not model_loaded:
                    await ensure_model_loaded()
                _active_consumers[cid] = {"job_id": str(job[0]["id"]), "task_id": str(job[0]["task_id"])}
                try:
                    await process_job(job[0])
                finally:
                    _active_consumers[cid] = {"idle": True}
            else:
                _active_consumers[cid] = {"idle": True}
                await asyncio.sleep(POLL)
        except Exception as e:
            log.exception("consumer %d: %s", cid, e)
            _active_consumers[cid] = {"error": str(e)}
            await asyncio.sleep(5)

async def poll():
    """Main loop: spawn N consumers, each claims and processes one job at a time.
    Also runs periodic stuck-job recovery and memory pruning."""
    global pending_jobs_cache
    last_prune = time.time()
    last_recovery = time.time()
    last_pending_count = time.time()
    tasks = []
    for cid in range(CONSUMERS):
        tasks.append(asyncio.create_task(_consumer(cid), name=f"consumer-{cid}"))
    log.info("Spawned %d consumers", CONSUMERS)
    while running:
        _heartbeat()
        try:
            now = time.time()
            if now - last_prune > 6 * 3600:
                await prune_memories(pool)
                last_prune = now
            # Periodic stuck-job recovery (every 60s)
            if now - last_recovery > 60.0:
                await recover_stuck_jobs(stale_seconds=300.0)
                last_recovery = now
            # Refresh pending job count (every 5s) for the status file
            if now - last_pending_count > 5.0:
                try:
                    pending_jobs_cache = await pool.fetchval(
                        "SELECT count(*) FROM proxy.memory_jobs WHERE status='pending'"
                    ) or 0
                except Exception:
                    pass
                last_pending_count = now
            await asyncio.sleep(1.0)
        except Exception as e:
            log.exception("poll loop: %s", e)
            await asyncio.sleep(5)
    # Cancel all consumers on shutdown
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("All %d consumers stopped", CONSUMERS)

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def _atomic_write(path: str, text: str) -> None:
    """Atomic write: temp file + os.replace so readers never see a torn file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        log.warning("atomic write failed for %s", path, e)

def _write_lock(pid: int, ts: float) -> None:
    try:
        _atomic_write(LOCK_FILE, "pid=%d\nheartbeat=%.3f\n" % (pid, ts))
    except OSError as e:
        log.warning("could not write lock file: %s", e)

def _write_status(force: bool = False) -> None:
    """Write a machine-readable status file (lag + heartbeat) for proxy + dashboard."""
    global _last_status_write
    now = time.time()
    if not force and (now - _last_status_write) < 5.0:
        return
    _last_status_write = now
    lag = (now - last_completion) if last_completion else 0.0
    # Count active (non-idle) consumers
    active = sum(1 for v in _active_consumers.values() if not v.get("idle") and not v.get("error"))
    # Calculate requests per minute (last 5 min window)
    rpm = len(lm_requests)
    # Context: average and max of recent requests
    ctx_avg = round(sum(lm_context_tokens) / len(lm_context_tokens)) if lm_context_tokens else 0
    ctx_max = max(lm_context_tokens) if lm_context_tokens else 0
    data = {
        "pid": os.getpid(),
        "heartbeat": now,
        "lag_seconds": round(lag, 2),
        "last_completion": last_completion,
        "jobs_done": jobs_done_total,
        "pending_jobs": pending_jobs_cache,
        "consumers_total": CONSUMERS,
        "consumers_active": active,
        "lm_rpm": rpm,
        "lm_latency_ms": lm_last_latency_ms,
        "lm_ctx_avg": ctx_avg,
        "lm_ctx_max": ctx_max,
        "lm_tokens_in_total": lm_total_tokens_in,
        "lm_tokens_out_total": lm_total_tokens_out,
    }
    _atomic_write(STATUS_FILE, json.dumps(data))

def _read_lock():
    try:
        with open(LOCK_FILE) as f:
            data = f.read()
        pid = int(re.search(r"pid=(\d+)", data).group(1))
        hb = float(re.search(r"heartbeat=([0-9.]+)", data).group(1))
        return pid, hb
    except (OSError, AttributeError, ValueError):
        return None, None

def _sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "/" + addr[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect(addr)
        s.sendall(msg.encode())
        s.close()
    except OSError:
        pass

def _heartbeat() -> None:
    global running
    if lock_fd is None:
        return
    _write_lock(os.getpid(), time.time())
    _write_status()
    _sd_notify("WATCHDOG=1")
    # Harden: if the lock file now shows a DIFFERENT live pid, we lost it (stale takeover).
    lp, _ = _read_lock()
    if lp is not None and lp != os.getpid():
        log.error("lost single-instance lock (now held by pid %d); shutting down to avoid double-poll", lp)
        running = False

def acquire_single_instance_lock() -> bool:
    """Acquire the single-instance lock. Returns False if another healthy
    worker holds it (caller should exit). Kills a frozen holder first."""
    global lock_fd
    pid = os.getpid()
    prior_pid, prior_hb = _read_lock()
    if prior_pid is not None and prior_pid != pid and _pid_alive(prior_pid):
        age = time.time() - prior_hb
        if age < LOCK_TTL:
            log.warning("another healthy worker (pid %d, heartbeat %.0fs ago) holds the lock; exiting", prior_pid, age)
            return False
        log.warning("worker pid %d is FROZEN (heartbeat %.0fs stale > %.0fs); killing it", prior_pid, age, LOCK_TTL)
        try:
            os.kill(prior_pid, signal.SIGKILL)
        except OSError as e:
            log.warning("could not kill frozen worker %d: %s", prior_pid, e)
        time.sleep(1)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log.warning("flock is held by a live worker; exiting (single-instance guard)")
        os.close(fd)
        return False
    _write_lock(pid, time.time())
    lock_fd = fd
    log.info("single-instance lock acquired (pid %d, lock %s)", pid, LOCK_FILE)
    return True

def release_single_instance_lock() -> None:
    global lock_fd
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except OSError:
            pass
        lock_fd = None

async def main():
    global pool, client, lock_fd
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
    if not acquire_single_instance_lock():
        return
    _sd_notify("READY=1")
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=max(5, CONSUMERS * 2))
    client = httpx.AsyncClient(headers={"Authorization": f"Bearer {LM_API_KEY}"}) if LM_API_KEY else httpx.AsyncClient()
    _write_status()
    # Recover stuck 'processing' jobs (from previous crash/restart)
    await recover_stuck_jobs(stale_seconds=0.0)  # 0 = recover ALL stuck jobs on startup
    log.info("Memory worker started (model=%s, url=%s, poll=%.1fs, consumers=%d, max_attempts=%d, outage_ttl=%.0fs, api_key=%s)",
              LM_MODEL, LM_URL, POLL, CONSUMERS, MAX_ATTEMPTS, OUTAGE_TTL, "set" if LM_API_KEY else "MISSING")
    try:
        await poll()
    finally:
        await client.aclose()
        await pool.close()
        release_single_instance_lock()
        log.info("Memory worker stopped")

if __name__ == "__main__":
    asyncio.run(main())
