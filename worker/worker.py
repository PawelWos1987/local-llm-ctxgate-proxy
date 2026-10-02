"""local-llm-ctxgate-proxy 4B Memory Worker (asynchronous durable-memory extraction).

Polls proxy.memory_jobs (FOR UPDATE SKIP LOCKED, parallelism=1), calls the
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
import json
import logging
import os
import re
import fcntl
import signal
import socket
import time
from typing import Any, Optional

import asyncpg
import threading
import httpx

# --- Configuration ---
LM_URL = os.environ.get("CTXGATE_LM_URL", "http://127.0.0.1:1234/v1/chat/completions")
LM_MODEL = os.environ.get("CTXGATE_LM_MODEL", "qwen3-4b-instruct-2507")
DSN = os.environ.get("CTXGATE_DB_DSN") or os.environ.get("CTXPROXY_DB_DSN") or "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy"
POLL = float(os.environ.get("CTXGATE_WORKER_POLL", "2.0"))
CONCURRENCY = int(os.environ.get("CTXGATE_WORKER_CONCURRENCY", "1"))
MAX_ATTEMPTS = int(os.environ.get("CTXGATE_WORKER_MAX_ATTEMPTS", "3"))
# Outage TTL: how long to keep retrying before marking jobs failed (seconds)
OUTAGE_TTL = float(os.environ.get("CTXGATE_WORKER_OUTAGE_TTL", "1800"))
# Base URL for model management endpoints (derived from LM_URL by default)
# Derive base URL: strip /v1/chat/completions or /v1 suffix
if "CTXGATE_LM_BASE" in os.environ:
    BASE_URL = os.environ["CTXGATE_LM_BASE"]
else:
    _u = LM_URL
    if _u.endswith("/chat/completions"):
        _u = _u[:-len("/chat/completions")]
    if _u.endswith("/v1"):
        _u = _u[:-len("/v1")]
    BASE_URL = _u
# Section 18 generation settings (established for this 4B deployment)
TEMP = 0.7
MIN_P = 0.05
TOP_P = 0.9
MAX_TOKENS = int(os.environ.get("CTXGATE_WORKER_MAX_TOKENS", "512"))
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
_last_status_write = 0.0  # whether we've confirmed the model is loaded this session
# Lag / completion tracking (source of the worker_lag_seconds metric)
last_completion: Optional[float] = None  # time.time() of the last successful job
jobs_done_total: int = 0


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
    return (
        "CURRENT TASK: " + (task_desc or "(none)") + "\n"
        "CURRENT WORKING MEMORY: " + (wm or "(empty)")[:WM_EXCERPT_CHARS] + "\n"
        "NEW EVENT (role=" + role + "): " + content + "\n"
        + ("TOOL RESULT EXCERPT: " + tool_txt + "\n" if tool_txt else "")
        + ("CHANGED FILES: " + changed + "\n" if changed else "")
        + "source_event_id: " + str(event.get("id", ""))
    )


# --- Pre-load: ensure model is loaded before first completion ---
async def ensure_model_loaded() -> bool:
    """Check if the model is loaded in LM Studio; if not, trigger a load and wait.
    
    This prevents the 120s request timeout from being consumed by model load time.
    Returns True if the model is (now) available, False if unreachable.
    """
    global model_loaded
    if model_loaded:
        return True
    try:
        # Check if model is already loaded
        r = await client.get(BASE_URL + "/v1/models", timeout=10)
        if r.status_code == 200:
            models = r.json().get("data", [])
            if any(m.get("id") == LM_MODEL for m in models):
                model_loaded = True
                log.info("Model %s already loaded", LM_MODEL)
                return True
        # Model not loaded - trigger load via LM Studio API
        log.info("Model %s not loaded, triggering load...", LM_MODEL)
        try:
            await client.post(BASE_URL + "/v1/models/load", json={"model_id": LM_MODEL}, timeout=30)
        except Exception:
            # Some LM Studio versions don't have a load endpoint;
            # the first completion call will auto-load. Just proceed.
            log.warning("No /v1/models/load endpoint; will rely on auto-load on first call")
            model_loaded = True
            return True
        # Poll for model to appear (up to 90s)
        for i in range(45):
            await asyncio.sleep(2)
            try:
                r2 = await client.get(BASE_URL + "/v1/models", timeout=10)
                if r2.status_code == 200:
                    models = r2.json().get("data", [])
                    if any(m.get("id") == LM_MODEL for m in models):
                        model_loaded = True
                        log.info("Model %s loaded after %ds", LM_MODEL, (i + 1) * 2)
                        return True
            except Exception:
                continue
        log.warning("Model %s not confirmed loaded after 90s; proceeding anyway", LM_MODEL)
        model_loaded = True  # give it a chance; the completion call will auto-load
        return True
    except Exception as e:
        model_loaded = False
        log.warning("ensure_model_loaded: LM Studio unreachable: %s (reset model_loaded for retry)", e)
        return False


# --- LM Studio call (json_schema enforced, no reasoning) ---
async def call_4b(payload: str) -> dict:
    body = {
        "model": LM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        "temperature": TEMP,
        "min_p": MIN_P,
        "top_p": TOP_P,
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "memory_worker", "strict": True, "schema": MEMORY_SCHEMA},
        },
    }
    r = await client.post(LM_URL, json=body, timeout=120)
    if r.status_code != 200:
        raise RuntimeError("LM Studio HTTP %d: %s" % (r.status_code, r.text[:200]))
    data = r.json()
    if "choices" not in data or not data["choices"]:
        raise RuntimeError("LM Studio response missing choices: %s" % str(data)[:200])
    content = data["choices"][0]["message"]["content"]
    if not content:
        raise RuntimeError("LM Studio returned empty content")
    return json.loads(content)


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
    """Ask the 4B to self-review its generated memory entries."""
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
    r = await client.post(LM_URL, json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError("Quality check HTTP %d: %s" % (r.status_code, r.text[:200]))
    return json.loads(r.json()["choices"][0]["message"]["content"])


# --- Deterministic dedupe / UPDATE / SUPERSEDE (section 12) ---
async def apply_memories(task_id: str, event_id: str, resp: dict) -> int:
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
        row = await pool.fetchrow(
            "SELECT id FROM proxy.memories WHERE task_id=$1 AND active=true AND category=$2 "
            "AND trim(regexp_replace(regexp_replace(lower(key),'[^a-z0-9]+',' ','g'),'[[:space:]]+',' ','g'))=$3 LIMIT 1",
            task_id, category, mkey,
        )
        if action == "SUPERSEDE":
            if row:
                nid = await pool.fetchval(
                    "INSERT INTO proxy.memories(task_id,key,value,category,importance,source_event_id,status,model_name) "
                    "VALUES($1,$2,$3,$4,$5,$6,'active',$7) RETURNING id",
                    task_id, title, content, category, imp, event_id, LM_MODEL,
                )
                await pool.execute(
                    "UPDATE proxy.memories SET active=false,status='superseded',superseded_by=$1,updated_at=now() WHERE id=$2",
                    nid, row["id"],
                )
                log.info("SUPERSEDE %s (old=%s new=%s)", title, row["id"], nid)
            else:
                await pool.execute(
                    "INSERT INTO proxy.memories(task_id,key,value,category,importance,source_event_id,status,model_name) "
                    "VALUES($1,$2,$3,$4,$5,$6,'active',$7)",
                    task_id, title, content, category, imp, event_id, LM_MODEL,
                )
                log.info("SUPERSEDE(no-old) -> INSERT %s", title)
            applied += 1
        elif action == "UPDATE":
            if row:
                await pool.execute(
                    "UPDATE proxy.memories SET value=$1,importance=$2,source_event_id=$3,updated_at=now() WHERE id=$4",
                    content, imp, event_id, row["id"],
                )
                log.info("UPDATE %s", title)
            else:
                await pool.execute(
                    "INSERT INTO proxy.memories(task_id,key,value,category,importance,source_event_id,status,model_name) "
                    "VALUES($1,$2,$3,$4,$5,$6,'active',$7)",
                    task_id, title, content, category, imp, event_id, LM_MODEL,
                )
                log.info("UPDATE(no-old) -> INSERT %s", title)
            applied += 1
        else:  # NEW
            if row:
                # Deterministic dedupe: same (type,title) already active -> refresh, don't duplicate
                await pool.execute(
                    "UPDATE proxy.memories SET value=$1,importance=$2,source_event_id=$3,updated_at=now() WHERE id=$4",
                    content, imp, event_id, row["id"],
                )
                log.info("NEW(dup) -> refresh %s", title)
            else:
                await pool.execute(
                    "INSERT INTO proxy.memories(task_id,key,value,category,importance,source_event_id,status,model_name) "
                    "VALUES($1,$2,$3,$4,$5,$6,'active',$7)",
                    task_id, title, content, category, imp, event_id, LM_MODEL,
                )
                log.info("NEW %s imp=%d", title, imp)
            applied += 1
    return applied


async def update_working_memory(task_id: str, su: dict):
    # Refresh WM for every substantive turn - not just when 'changed' is true.
    # The 4B always returns current_state; we use it to keep WM fresh.
    state = su.get("current_state") or ""
    subtask = su.get("current_subtask") or ""
    if not state and not subtask:
        return
    content = "STATE: " + state + (" | SUBTASK: " + subtask if subtask else "")
    await pool.execute(
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


async def claim_job():
    """Claim exactly ONE pending job (parallelism=1) with row locking."""
    return await pool.fetchrow(
        "SELECT id, task_id, event_id, attempts FROM proxy.memory_jobs "
        "WHERE status='pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    )


async def process_job(job) -> None:
    """Process a single memory job with outage-aware retry logic.
    
    On LM Studio unreachable (connection error):
    - If within OUTAGE_TTL: requeue as pending with exponential backoff
    - If past OUTAGE_TTL: mark as failed
    On other errors: use standard MAX_ATTEMPTS bounded retry
    """
    global outage_since, last_completion, jobs_done_total
    jid, task_id, event_id = str(job["id"]), str(job["task_id"]), str(job["event_id"]) if job["event_id"] else None
    await pool.execute("UPDATE proxy.memory_jobs SET status='processing',started_at=now() WHERE id=$1", jid)
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
        resp = await call_4b(payload)
        # Success: reset outage tracker
        outage_since = None

        # --- Quality-check loop: self-review, retry once, discard if bad again ---
        acts = resp.get("memory_actions", [])
        if acts:  # only check if there are entries to review
            for qc_attempt in range(QC_MAX_RETRIES + 1):  # 0=initial check, 1=retry check
                qc = await call_4b_quality_check(acts)
                if qc.get("quality") == "good":
                    log.info("Quality check PASSED (attempt %d): %s", qc_attempt + 1, qc.get("reason", ""))
                    break
                log.warning("Quality check FAILED (attempt %d/%d): %s", qc_attempt + 1, QC_MAX_RETRIES + 1, qc.get("reason", ""))
                if qc_attempt < QC_MAX_RETRIES:
                    # Regenerate
                    resp = await call_4b(payload)
                    acts = resp.get("memory_actions", [])
                else:
                    # Bad again -> discard all entries
                    log.info("Quality check failed twice -> discarding all memory entries")
                    resp = {"memory_actions": [], "state_update": resp.get("state_update", {"changed": False, "current_state": None, "current_subtask": None})}
                    acts = []

        if not validate_response(resp):
            raise ValueError("4B response failed schema validation")
        applied = await apply_memories(task_id, event_id or jid, resp)
        await update_working_memory(task_id, resp.get("state_update", {}))
        await pool.execute(
            "UPDATE proxy.memory_jobs SET status='done',completed_at=now(),result=$1,attempts=$2 WHERE id=$3",
            json.dumps(resp), int(job.get("attempts") or 0) + 1, jid,
        )
        log.info("JOB done %s (applied=%d)", jid, applied)
        last_completion = time.time()
        jobs_done_total += 1
        _write_status(force=True)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout) as e:
        # LM Studio unreachable/outage - use TTL-based backoff
        now = time.time()
        if outage_since is None:
            outage_since = now
            log.warning("LM Studio outage STARTED (job %s): %s", jid, e)
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
        # Non-outage error: standard bounded retry
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


async def poll():
    """Main poll loop with pre-load before first completion in a batch."""
    last_prune = time.time()
    while running:
        _heartbeat()
        try:
            # Slow prune cycle (~6h): never in the hot path
            now = time.time()
            if now - last_prune > 6 * 3600:
                await prune_memories(pool)
                last_prune = now
            job = await claim_job()
            if job is not None:
                # Pre-load: before the first completion, ensure model is loaded
                if not model_loaded:
                    await ensure_model_loaded()
                await process_job(job)
            else:
                await asyncio.sleep(POLL)
        except Exception as e:
            log.exception("poll loop: %s", e)
            await asyncio.sleep(5)


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
    global _last_status_write
    _now = time.time()
    if not force and (_now - _last_status_write) < 5.0:
        return
    _last_status_write = _now
    """Write a machine-readable status file (lag + heartbeat) for proxy + dashboard."""
    now = time.time()
    lag = (now - last_completion) if last_completion else 0.0
    data = {
        "pid": os.getpid(),
        "heartbeat": now,
        "lag_seconds": round(lag, 2),
        "last_completion": last_completion,
        "jobs_done": jobs_done_total,
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
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    client = httpx.AsyncClient()
    _write_status()
    # Recover stuck 'processing' jobs (from previous crash/restart)
    recovered = await pool.fetch(
        "UPDATE proxy.memory_jobs SET status='pending', started_at=NULL WHERE status='processing' RETURNING id"
    )
    if recovered:
        log.info("Recovered %d stuck 'processing' jobs back to 'pending'", len(recovered))
    log.info("4B memory worker started (model=%s, poll=%.1fs, max_attempts=%d, outage_ttl=%.0fs, concurrency=%d)",
             LM_MODEL, POLL, MAX_ATTEMPTS, OUTAGE_TTL, CONCURRENCY)
    try:
        await poll()
    finally:
        await client.aclose()
        await pool.close()
        release_single_instance_lock()
        log.info("4B memory worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
