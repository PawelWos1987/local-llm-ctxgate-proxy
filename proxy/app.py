"""local-llm-ctxgate-proxy: Context gate proxy for Goose -> vLLM with PG memory."""
import hashlib
import os
import sys
import json
import logging
import time
import uuid
from typing import Any, Optional
from contextlib import asynccontextmanager

import asyncpg
import httpx
import tiktoken
import tokenizers
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ctxgate-proxy")

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError) as e:
        log.warning("env %s=%r is not a valid int; using default %d (%s)", name, raw, default, e)
        return default

# --- Constants ---
VLLM_URL = os.environ.get("CTXGATE_VLLM_URL", "http://127.0.0.1:29000/v1")
VLLM_MODEL = os.environ.get("CTXGATE_VLLM_MODEL", "Qwen3.8-27B")
MAX_CONTEXT = _env_int("CTXGATE_MAX_CONTEXT", 84000)
MAX_INPUT = _env_int("CTXGATE_MAX_INPUT", 64000)
MAX_OUTPUT = _env_int("CTXGATE_MAX_OUTPUT", 18000)
SAFETY_MARGIN = _env_int("CTXGATE_SAFETY_MARGIN", 2000)
MEMORY_TTL_DAYS = _env_int("CTXGATE_MEMORY_TTL_DAYS", 90)
DB_DSN = os.environ.get("CTXGATE_DB_DSN") or os.environ.get("CTXPROXY_DB_DSN") or "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy"
PROXY_PORT = _env_int("CTXGATE_PROXY_PORT", 9200)
API_KEY = os.environ.get("CTXGATE_API_KEY", "")
MAX_BODY_BYTES = _env_int("CTXGATE_MAX_BODY_BYTES", 20 * 1024 * 1024)
QWEN_TOKENIZER_PATH = os.environ.get("CTXGATE_QWEN_TOKENIZER", "/home/user/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json")


# --- Global state (per-session where applicable) ---
pool: Optional[asyncpg.Pool] = None
enc: Optional[Any] = None

# Per-session state, keyed by session_key = "{x_session_id}:{content_fp[:8]}"
session_fingerprints: dict[str, str] = {}
session_tokens: dict[str, dict] = {}  # {in, out, reqs, max_ctx}
recent_calls: list[dict] = []        # ring buffer, max 200
RECENT_CALLS_MAX = 200

metrics = {
    "requests_total": 0,
    "requests_ok": 0,
    "requests_error": 0,
    "trim_events": 0,
    "prefix_invalidations": 0,
    "toolcall_strips": 0,
    "tokens_in_total": 0,
    "tokens_out_total": 0,
    "max_context_seen": 0,
    "reasoning_strips": 0,
    "started_at": time.time(),
}

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

def _save_injection_metrics() -> None:
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
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    tok_name = "cl100k_base (fallback)"
    try:
        enc = tokenizers.Tokenizer.from_file(QWEN_TOKENIZER_PATH)
        tok_name = "Qwen tokenizer.json"
        try:
            vocab = enc.get_vocab_size()
        except Exception:
            vocab = -1
        log.info("ctxgate-proxy started: tokenizer=%s vocab=%d, vllm=%s model=%s, api_key=%s",
                 QWEN_TOKENIZER_PATH, vocab, VLLM_URL, VLLM_MODEL, "set" if API_KEY else "off")
    except Exception as e:
        enc = tiktoken.get_encoding("cl100k_base")
        log.warning("ctxgate-proxy: Qwen tokenizer load FAILED (%s), falling back to cl100k_base", e)
    yield
    await pool.close()
    log.info("ctxgate-proxy shutdown complete (graceful: pool drained)")

app = FastAPI(title="local-llm-ctxgate-proxy", version="0.2.0", lifespan=lifespan)

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
    total = 0
    for m in messages:
        total += count_message_tokens(m)
    total += len(messages) * 4
    return total

# --- Session key (D11 per-session) ---
def make_session_key(x_session_id: str, messages: list) -> str:
    """Derive a unique session key from provider identity + content fingerprint.
    
    x_session_id: static per provider (from X-Session-ID header)
    content_fp:   derived from system prompt + first user message
    Result: same provider + same conversation topic = same key
            same provider + different topic = different key
            different provider = different key
    """
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
    raw = "\x00".join(parts)
    fp = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"{x_session_id}:{fp}"

# --- Prefix fingerprint (per-session) ---
def compute_prefix_fingerprint(messages: list) -> str:
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
    raw = "\x00".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def check_prefix(session_key: str, messages: list) -> None:
    """Log WARNING if prefix fingerprint changes for this session. Per-session state."""
    fp = compute_prefix_fingerprint(messages)
    prev = session_fingerprints.get(session_key)
    if prev is not None and fp != prev:
        metrics["prefix_invalidations"] += 1
        log.warning("PREFIX INVALIDATED session=%s (old=%s new=%s)", session_key, prev, fp)

# --- D10: Reasoning stripping ---
def strip_reasoning(messages: list) -> list:
    cleaned = []
    for m in messages:
        if m.get("role") == "assistant" and "reasoning" in m:
            m = {k: v for k, v in m.items() if k != "reasoning"}
        cleaned.append(m)
    return cleaned

# --- D9: Malformed tool-call sanitization ---
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
def sanitize_for_vllm(messages: list) -> list:
    """Make messages vLLM-safe: content None -> empty, ensure >=1 user message."""
    out = []
    for m in messages:
        m = dict(m)
        if m.get("content") is None:
            m["content"] = ""
        out.append(m)
    if not any(m.get("role") == "user" for m in out):
        sysc = ""
        for m in out:
            if m.get("role") == "system":
                sc = m.get("content", "")
                if isinstance(sc, list):
                    sc = " ".join(p.get("text", "") for p in sc if isinstance(p, dict))
                sysc = sc
                break
        out.append({"role": "user", "content": sysc or "Please proceed."})
    return out
async def build_context(request_messages: list) -> list:
    messages = strip_reasoning([m.copy() for m in sanitize_for_vllm(request_messages)])
    sanitized = []
    for m in messages:
        m, stripped = sanitize_tool_calls(m)
        sanitized.append(m)
    messages = sanitized
    total = count_messages_tokens(messages)
    log.info("Context: %d messages, %d tokens (limit %d)", len(messages), total, MAX_INPUT)
    if total > MAX_INPUT:
        log.warning("Context over limit: %d > %d, trimming", total, MAX_INPUT)
        messages = trim_context(messages, MAX_INPUT)
        metrics["trim_events"] += 1
        total = count_messages_tokens(messages)
        log.info("After trim: %d messages, %d tokens", len(messages), total)
    return messages

def trim_context(messages: list, max_tokens: int) -> list:
    if len(messages) <= 3:
        return messages
    system_msgs = [m for m in messages if m.get("role") == "system"]
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    keep_head = []
    if system_msgs:
        keep_head.extend(system_msgs)
    if first_user:
        keep_head.append(first_user)
    head_tokens = count_messages_tokens(keep_head)
    tail_budget = max_tokens - head_tokens - 100
    tail = []
    tail_tokens = 0
    for m in reversed(messages):
        if id(m) in [id(x) for x in keep_head]:
            continue
        mt = count_message_tokens(m)
        if tail_tokens + mt > tail_budget:
            break
        tail.insert(0, m)
        tail_tokens += mt
    result = keep_head + tail
    log.info("Trimmed: %d -> %d messages", len(messages), len(result))
    return result

# --- Per-session token tracking ---
def _track_session_tokens(session_key: str, input_tokens: int, output_tokens: int = 0, count_req: bool = True):
    """Update per-session token counters. count_req=False for output-only updates."""
    if session_key not in session_tokens:
        session_tokens[session_key] = {"in": 0, "out": 0, "reqs": 0, "max_ctx": 0}
    st = session_tokens[session_key]
    st["in"] += input_tokens
    st["out"] += output_tokens
    if count_req:
        st["reqs"] += 1
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

def extract_knowledge(session_id: str, session_key: str, messages: list) -> list:
    """Deterministic extraction of high-signal knowledge from conversation."""
    items = []
    seen = set()
    for m in messages:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            continue
        for match in _re.finditer(r'(?:the|a|an)?\s*([a-z_][a-z0-9_]*)\s+(?:is|equals|is set to|set to|\=)\s+(.{2,200})', content, _re.IGNORECASE):
            k = match.group(1).strip().lower()
            v = match.group(2).strip().rstrip('.')
            if len(v) < 3:
                continue
            if k in ("that","this","it","was","are","be","would","could","should","can","will","have","has","had","do","does","did","not","no","yes","ok","okay","fine","good","great"):
                continue
            key = (k, v[:100])
            if key not in seen:
                seen.add(key)
                items.append({"domain": "fact", "key": k, "value": v[:200], "importance": 5})
        for match in _re.finditer(r'(?:decided to|going with|will use|using|chose|chooses)\s+(.{2,150})', content, _re.IGNORECASE):
            v = match.group(1).strip().rstrip('.')
            if len(v) < 3:
                continue
            key = ("decision", v[:80])
            if key not in seen:
                seen.add(key)
                items.append({"domain": "decision", "key": v[:60].lower(), "value": v[:200], "importance": 7})
        for match in _re.finditer(r'(?:prefer|preference for|always use|I like)\s+(.{2,100})', content, _re.IGNORECASE):
            v = match.group(1).strip().rstrip('.')
            if len(v) < 3:
                continue
            key = ("preference", v[:60])
            if key not in seen:
                seen.add(key)
                items.append({"domain": "preference", "key": v[:50].lower(), "value": v[:200], "importance": 6})
        for match in _re.finditer(r'(port|url|model|threshold|limit|timeout|max_\w+|min_\w+)\s*(?:is|\=|set to|:)?\s*([\w./:]+-?[\w./:=\-]*)', content, _re.IGNORECASE):
            k = match.group(1).strip().lower()
            v = match.group(2).strip()
            if len(v) < 2:
                continue
            key = (k, v)
            if key not in seen:
                seen.add(key)
                items.append({"domain": "config", "key": k, "value": v[:200], "importance": 8})
    return items[:10]

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
            parts.append(c)
    return " ".join(parts).lower()


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


async def fetch_task_memory(session_id: str, messages: list, wm_budget: int = 800, mem_budget: int = 1200, total_budget: int = 2000) -> str:
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
    task_uuid = await _resolve_task(session_id, create=False)
    if task_uuid is None:
        return ""
    blob = _context_blob(messages)
    terms = _extract_terms(messages)
    parts = []
    total = 0

    # 1. Working memory (current working state) - inject only if not already in context
    wrow = await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_uuid)
    wm_text = (wrow["content"].strip() if wrow and wrow["content"] else "")
    if wm_text and not _already_in_context(wm_text, blob):
        line = "WORKING MEMORY: " + wm_text
        t = count_tokens(line)
        if t <= wm_budget and total + t <= total_budget:
            parts.append(line)
            total += t

    # 2. Durable memories: CRITICAL (small safety net) + relevant HIGH
    rows = []
    if total < total_budget:
        crit = await pool.fetch("SELECT id, key, value FROM proxy.memories WHERE task_id=$1 AND active=true AND importance=10 ORDER BY updated_at DESC LIMIT 5", task_uuid)
        rel = []
        if terms:
            rel = await pool.fetch("SELECT id, key, value FROM proxy.memories WHERE task_id=$1 AND active=true AND (key ILIKE ANY($2) OR value ILIKE ANY($2)) ORDER BY importance DESC, updated_at DESC LIMIT 8", task_uuid, ["%" + t + "%" for t in list(terms)[:20]])
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

    # Touch-on-use: stamp last_accessed_at for injected memory rows
    try:
        if injected_ids:
            await pool.execute("UPDATE proxy.memories SET last_accessed_at = now() WHERE id = ANY($1)", injected_ids)
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


def _record_call(session_key: str, input_tokens: int, output_tokens: int, status: str, model: str, stream: bool, detail: str = ""):
    """Add to ring buffer of recent calls."""
    entry = {
        "ts": time.time(),
        "ts_human": time.strftime("%H:%M:%S", time.localtime()),
        "session": session_key,
        "in": input_tokens,
        "out": output_tokens,
        "status": status,
        "model": model,
        "stream": stream,
        "detail": (detail or "")[:300],
        "explanation": explain_status(status, detail),
    }
    recent_calls.append(entry)
    if len(recent_calls) > RECENT_CALLS_MAX:
        recent_calls.pop(0)

# --- Health endpoint ---
@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.2.0", "sessions": len(session_fingerprints)}

# --- Metrics endpoints ---
@app.get("/metrics")
async def get_metrics():
    uptime = time.time() - metrics["started_at"]
    result = dict(metrics)
    result["uptime_human"] = _human_time(uptime)
    result["started_human"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(metrics["started_at"]))
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
    lines = [
        "# TYPE ctxgate_requests_total counter", "ctxgate_requests_total %d" % metrics["requests_total"],
        "# TYPE ctxgate_requests_ok counter", "ctxgate_requests_ok %d" % metrics["requests_ok"],
        "# TYPE ctxgate_requests_error counter", "ctxgate_requests_error %d" % metrics["requests_error"],
        "# TYPE ctxgate_trim_events counter", "ctxgate_trim_events %d" % metrics["trim_events"],
        "# TYPE ctxgate_prefix_invalidations counter", "ctxgate_prefix_invalidations %d" % metrics["prefix_invalidations"],
        "# TYPE ctxgate_toolcall_strips counter", "ctxgate_toolcall_strips %d" % metrics["toolcall_strips"],
        "# TYPE ctxgate_reasoning_strips counter", "ctxgate_reasoning_strips %d" % metrics["reasoning_strips"],
        "# TYPE ctxgate_tokens_in_total counter", "ctxgate_tokens_in_total %d" % metrics["tokens_in_total"],
        "# TYPE ctxgate_tokens_out_total counter", "ctxgate_tokens_out_total %d" % metrics["tokens_out_total"],
        "# TYPE ctxgate_max_context_seen gauge", "ctxgate_max_context_seen %d" % metrics["max_context_seen"],
        "# TYPE ctxgate_active_sessions gauge", "ctxgate_active_sessions %d" % len(session_fingerprints),
        "# TYPE ctxgate_uptime_seconds gauge", "ctxgate_uptime_seconds %.1f" % uptime,
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain")

@app.get("/api/metrics")
async def api_metrics():
    """Structured metrics for dashboard (JSON)."""
    uptime = time.time() - metrics["started_at"]
    return {
        **metrics,
        "uptime_sec": round(uptime, 1),
        "uptime_human": _human_time(uptime),
        "started_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(metrics["started_at"])),
        "active_sessions": len(session_fingerprints),
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
        })
    sessions.sort(key=lambda s: s["requests"], reverse=True)
    return sessions

@app.get("/api/recent-calls")
async def api_recent_calls(n: int = 50):
    """Recent call ring buffer."""
    return list(reversed(recent_calls[-n:]))

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
    return {"status": "reset", "cleared_sessions": 0}

# --- Main chat completions endpoint ---
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
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

    # --- Session key: provider identity + content fingerprint ---
    x_sid = request.headers.get('X-Session-ID', 'unknown')
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
        import asyncio
        asyncio.ensure_future(_enqueue_memory_job(x_sid, last_user_content))

    # Build context
    built = await build_context(messages)

    # Safe defaults: guard _record_injection against a fetch exception leaving tm/kn unset
    tm = ""
    kn = ""

    # --- Cross-session knowledge injection ---
    try:
        kn = await fetch_relevant_knowledge(messages, max_items=5, max_tokens=400)
        if kn:
            has_system = any(m.get("role") == "system" for m in built)
            if has_system:
                for i, m in enumerate(built):
                    if m.get("role") == "system":
                        built[i] = {**m, "content": m.get("content", "") + "\n\n" + kn}
                        break
            else:
                built.insert(0, {"role": "system", "content": kn})
    except Exception as e:
        log.warning("Knowledge injection failed: %s", e)

    # --- Per-task durable memory injection (section 13) ---
    try:
        tm = await fetch_task_memory(x_sid, messages)
        if tm:
            has_system = any(m.get("role") == "system" for m in built)
            if has_system:
                for i, m in enumerate(built):
                    if m.get("role") == "system":
                        built[i] = {**m, "content": m.get("content", "") + "\"\"" + tm}
                        break
            else:
                built.insert(0, {"role": "system", "content": tm})
    except Exception as e:
        log.warning("Task memory injection failed: %s", e)

    # --- Injection / utilization instrumentation (lightweight, no DB) ---
    _record_injection(x_sid, tm, kn)

    # Per-session prefix check
    check_prefix(session_key, built)
    fp = compute_prefix_fingerprint(built)
    session_fingerprints[session_key] = fp

    # Token tracking
    input_tokens = count_messages_tokens(built)
    metrics["max_context_seen"] = max(metrics.get("max_context_seen", 0), input_tokens)
    metrics["tokens_in_total"] += input_tokens
    _track_session_tokens(session_key, input_tokens)

    # --- Cross-session knowledge extraction (deterministic, async) ---
    try:
        k_items = extract_knowledge(x_sid, session_key, messages)
        if k_items:
            import asyncio
            asyncio.ensure_future(store_knowledge(k_items, x_sid, session_key))
    except Exception as e:
        log.warning("Knowledge extraction failed: %s", e)

    max_tokens = body.get("max_tokens", MAX_OUTPUT)
    max_tokens = min(max_tokens, MAX_OUTPUT)
    stream = body.get("stream", False)
    vllm_body = {
        "model": VLLM_MODEL,
        "messages": built,
        "max_tokens": max_tokens,
        "temperature": body.get("temperature", 0.7),
        "stream": stream,
    }
    if stream:
        vllm_body["stream_options"] = {"include_usage": True}
    if body.get("tools"):
        vllm_body["tools"] = body["tools"]
    if body.get("tool_choice"):
        vllm_body["tool_choice"] = body["tool_choice"]

    if stream:
        return await stream_to_vllm(vllm_body, input_tokens, session_key)
    else:
        return await forward_to_vllm(vllm_body, input_tokens, session_key)

async def forward_to_vllm(vllm_body: dict, input_tokens: int, session_key: str):
    global metrics
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(VLLM_URL + "/chat/completions", json=vllm_body)
            if resp.status_code != 200:
                metrics["requests_error"] += 1
                log.error("vLLM error %d: %s", resp.status_code, resp.text[:500])
                _record_call(session_key, input_tokens, 0, f"vllm_{resp.status_code}", VLLM_MODEL, False, resp.text[:300])
                return JSONResponse({"error": {"message": "vLLM " + str(resp.status_code), "explanation": explain_status(f"vllm_{resp.status_code}", resp.text[:300])}}, status_code=resp.status_code)
            data = resp.json()
            choices = data.get("choices", [])
            for choice in choices:
                msg = choice.get("message", {})
                if msg.get("tool_calls"):
                    cleaned, stripped = sanitize_tool_calls(msg)
                    if stripped:
                        metrics["toolcall_strips"] += 1
                        choice["message"]["tool_calls"] = None
                        choice["finish_reason"] = "stop"
            output_tokens = data.get("usage", {}).get("completion_tokens", 0)
            metrics["tokens_out_total"] += output_tokens
            metrics["requests_ok"] += 1
            _track_session_tokens(session_key, 0, output_tokens, count_req=False)
            _record_call(session_key, input_tokens, output_tokens, "ok", VLLM_MODEL, False)
            return JSONResponse(data)
    except httpx.TimeoutException:
        metrics["requests_error"] += 1
        _record_call(session_key, input_tokens, 0, "timeout", VLLM_MODEL, False, "vLLM 300s timeout")
        return JSONResponse({"error": {"message": "vLLM timeout", "explanation": explain_status("timeout")}}, status_code=504)
    except Exception as e:
        metrics["requests_error"] += 1
        log.exception("vLLM forward error: %s", e)
        _record_call(session_key, input_tokens, 0, "error", VLLM_MODEL, False, str(e)[:300])
        return JSONResponse({"error": {"message": str(e), "explanation": explain_status("error", str(e)[:300])}}, status_code=500)

async def stream_to_vllm(vllm_body: dict, input_tokens: int, session_key: str):
    global metrics
    async def generate():
        global metrics
        output_tokens = 0
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", VLLM_URL + "/chat/completions", json=vllm_body) as resp:
                    if resp.status_code != 200:
                        body_bytes = await resp.aread()
                        metrics["requests_error"] += 1
                        _record_call(session_key, input_tokens, 0, f"vllm_{resp.status_code}", VLLM_MODEL, True, body_bytes[:300].decode("utf-8", errors="replace"))
                        yield "data: " + json.dumps({"error": body_bytes[:200].decode("utf-8", errors="replace")}) + "\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            # Rewrite usage chunk so prompt_tokens is authoritative
                            try:
                                chunk = json.loads(data_str)
                                usage = chunk.get("usage")
                                if usage:
                                    if usage.get("completion_tokens"):
                                        output_tokens = usage["completion_tokens"]
                                    usage["prompt_tokens"] = input_tokens
                                    usage["total_tokens"] = input_tokens + (usage.get("completion_tokens") or 0)
                                    data_str = json.dumps(chunk)
                            except (json.JSONDecodeError, ValueError):
                                pass
                            yield "data: " + data_str + "\n\n"
                    yield "data: [DONE]\n\n"
                    metrics["requests_ok"] += 1
                    if output_tokens:
                        metrics["tokens_out_total"] += output_tokens
                        _track_session_tokens(session_key, 0, output_tokens, count_req=False)
                    _record_call(session_key, input_tokens, output_tokens, "ok", VLLM_MODEL, True)
        except httpx.TimeoutException:
            metrics["requests_error"] += 1
            _record_call(session_key, input_tokens, output_tokens, "timeout", VLLM_MODEL, True, "vLLM 300s timeout (stream)")
            yield "data: [DONE]\n\n"
        except Exception as e:
            metrics["requests_error"] += 1
            log.exception("Stream error: %s", e)
            _record_call(session_key, input_tokens, output_tokens, "error", VLLM_MODEL, True, str(e)[:300])
            yield "data: [DONE]\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")

async def _enqueue_memory_job(session_id, user_content):
    if os.environ.get("CTXGATE_MEMORY_WORKER", "1") == "0":
        return
    # --- Boilerplate filter: strip <turn-context> blocks ---
    import re
    cleaned = re.sub(r'<turn-context>.*?</turn-context>', '', user_content, flags=re.DOTALL).strip()
    if not cleaned or len(cleaned) < 30:
        return
    try:
        task_uuid = await _resolve_task(session_id, create=True)
        if task_uuid is None:
            return
        # --- Dedup: skip if last event has same content fingerprint ---
        last_row = await pool.fetchrow(
            'SELECT content FROM proxy.events WHERE task_id=$1 ORDER BY seq DESC, id DESC LIMIT 1',
            task_uuid)
        if last_row:
            last_fp = hashlib.sha256(last_row['content'][:5000].encode()).hexdigest()[:16]
            new_fp = hashlib.sha256(cleaned[:5000].encode()).hexdigest()[:16]
            if last_fp == new_fp:
                log.debug('Dedup: skipping duplicate enqueue for session %s', session_id)
                return
        # --- Seq fix: COALESCE(MAX(seq),-1)+1 ---
        seq_row = await pool.fetchrow(
            'SELECT COALESCE(MAX(seq),-1) + 1 AS ns FROM proxy.events WHERE task_id=$1', task_uuid)
        ns = seq_row['ns'] if seq_row else 0
        ev_id = await pool.fetchval(
            'INSERT INTO proxy.events (task_id, seq, role, content) VALUES ($1,$2,$3,$4) RETURNING id',
            task_uuid, ns, 'user', cleaned[:5000])
        await pool.execute(
            'INSERT INTO proxy.memory_jobs (task_id, event_id, status) VALUES ($1,$2,$3)',
            task_uuid, ev_id, 'pending')
        log.info('Enqueued memory job for session %s (seq %d)', session_id, ns)
    except Exception as e:
        log.warning('Memory job enqueue failed %s: %s', session_id, e)
async def _resolve_task(task_ref: str, create: bool = False):
    row = await pool.fetchrow("SELECT id FROM proxy.tasks WHERE session_id = $1", task_ref)
    if row:
        return str(row["id"])
    if not create:
        return None
    await pool.execute(
        "INSERT INTO proxy.tasks (session_id) VALUES ($1) ON CONFLICT (session_id) DO UPDATE SET updated_at = now()",
        task_ref,
    )
    row = await pool.fetchrow("SELECT id FROM proxy.tasks WHERE session_id = $1", task_ref)
    return str(row["id"])

# --- Knowledge sharing API ---
@app.post("/knowledge")
async def knowledge_create(request: Request):
    """Create or update a knowledge item. Cross-session, global."""
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
    total = await pool.fetchval("SELECT COUNT(*) FROM proxy.knowledge WHERE active = true")
    by_domain = await pool.fetch(
        "SELECT domain, COUNT(*) as cnt FROM proxy.knowledge WHERE active = true GROUP BY domain ORDER BY cnt DESC"
    )
    return {"total": total, "by_domain": [{"domain": r["domain"], "count": r["cnt"]} for r in by_domain]}

@app.post("/memory/inject")
async def memory_inject(request: Request):
    body = await request.json()
    task_ref = body.get("task_id") or body.get("task_ref")
    content = body.get("content", "")
    if not task_ref:
        return JSONResponse({"error": "task_id required"}, status_code=400)
    task_uuid = await _resolve_task(task_ref, create=True)
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

@app.get("/api/lmstudio")
async def api_lmstudio():
    """LM Studio 4B model status and stats."""
    import subprocess
    result = {"available": False, "model": "qwen3-4b-instruct-2507", "engine": "LM Studio (CPU)", "port": 1234}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("http://127.0.0.1:1234/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                model_ids = [m["id"] for m in data.get("data", [])]
                result["available"] = True
                result["models_loaded"] = model_ids
                result["model_count"] = len(model_ids)
                # Find the 4B model
                for mid in model_ids:
                    if "4b" in mid.lower():
                        result["active_4b"] = mid
                        break
    except Exception:
        result["available"] = False
        result["error"] = "LM Studio not reachable"
    # Process stats
    try:
        proc = subprocess.run(["ps", "-eo", "pid,comm,%cpu,%mem,rss,etime", "--no-headers"], capture_output=True, text=True, timeout=5)
        for line in proc.stdout.split("\n"):
            if "lm-studio" in line:
                parts = line.split()
                if len(parts) >= 6:
                    result["pid"] = parts[0]
                    result["cpu_pct"] = parts[2]
                    result["mem_pct"] = parts[3]
                    result["rss_mb"] = round(int(parts[4]) / 1024, 0)
                    result["elapsed"] = parts[5]
                break
    except Exception:
        pass
    return result


@app.get("/api/gpu")
async def api_gpu():
    """GPU status from nvidia-smi."""
    import subprocess
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        gpus = []
        for line in proc.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 4:
                gpus.append({
                    "name": parts[0],
                    "total": parts[1].replace(" MiB", ""),
                    "used": parts[2].replace(" MiB", ""),
                    "util": parts[3].replace(" %", ""),
                })
        return {"gpus": gpus}
    except Exception:
        return {"gpus": []}

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
        "model": os.environ.get("CTXGATE_LM_MODEL", "qwen3-4b-instruct-2507"),
        "lm_url": os.environ.get("CTXGATE_LM_URL", "http://127.0.0.1:1234/v1/chat/completions"),
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
        <thead><tr><th>Time</th><th>Session</th><th>In</th><th>Out</th><th>Status</th><th>Stream</th></tr></thead>
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
        <thead><tr><th>Key</th><th>Provider</th><th>Req</th><th>Tokens In</th><th>Tokens Out</th><th>Max Ctx</th></tr></thead>
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

    const cards = [
      { label: 'Requests', value: m.requests_total, sub: m.requests_ok + ' ok / ' + m.requests_error + ' err', cls: 'blue' },
      { label: 'Tokens In', value: fmtNum(m.tokens_in_total), sub: 'max ctx: ' + fmtNum(m.max_context_seen), cls: '' },
      { label: 'Tokens Out', value: fmtNum(m.tokens_out_total), sub: 'total generated', cls: 'green' },
      { label: 'Trim Events', value: m.trim_events, sub: 'prefix inv: ' + m.prefix_invalidations, cls: m.trim_events > 0 ? 'yellow' : '' },
      { label: 'Sessions', value: sessions.length, sub: 'active tracked', cls: 'blue' },
      { label: 'Uptime', value: m.uptime_human, sub: 'since ' + m.started_human, cls: '' },
    ];
    document.getElementById('stat-cards').innerHTML = cards.map(c =>
      '<div class="card ' + c.cls + '"><div class="label">' + c.label + '</div><div class="value">' + c.value + '</div><div class="sub">' + c.sub + '</div></div>'
    ).join('');

    const labels = calls.map(c => c.ts_human);
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
      '<tr><td>' + c.ts_human + '</td><td>' + esc(c.session) + '</td><td>' + fmtNum(c.in) + '</td><td>' + fmtNum(c.out) + '</td><td>' + badge(c.status) + '</td><td>' + (c.stream ? 'yes' : 'no') + '</td></tr>'
    ).join('');

    document.querySelector('#sessions-table tbody').innerHTML = sessions.map(s =>
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
  sel.innerHTML = '<option value="">All sessions</option>' + (a.sessions || []).map(s => '<option value="' + esc(s) + '">' + esc(s) + '</option>').join('');
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
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PROXY_PORT, log_level="info", timeout_graceful_shutdown=30)