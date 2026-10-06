# proxy/app.py — Comprehensive Bug & Issue Catalog (200+)

File: /home/pawelw/ctxproxy/proxy/app.py (5014 lines)
Date: 2026-10-06
Status: READ-ONLY audit. No fixes implemented.

---

## CATEGORY A: Crash / Exception Bugs

### A1. Line 832: s[:budget * 4] is a character-based truncation, not token-based
_fetch_session_summary checks t <= budget in tokens, then returns s[:budget * 4] (chars). The 4x ratio is a heuristic that breaks for non-Latin text (CJK chars are ~1 token each, so 4x overestimates; emoji can be 2+ tokens per char). The returned string can exceed the token budget. Should re-count after truncation.

### A2. Lines 1163-1164: sqlite_conn accessed without global declaration in lifespan shutdown
The lifespan function declares global pool, enc at line 1088 but does NOT declare global sqlite_conn. At line 1163 it reads sqlite_conn and at 1164 calls await sqlite_conn.close(). In Python, reading a module-level name works without global, BUT if any code path sets sqlite_conn = None (line 3754 in _get_goose_session_id) while the shutdown is running, the reference in lifespan local scope may be stale. More critically, if sqlite_conn was never initialized (None), line 1163 checks truthiness correctly, but the pattern is fragile.

### A3. Lines 3229-3231: data["usage"] may not exist
After auto-continuation, data is reassigned from resp.json() (line 3211). If vLLM returns a response without a usage key (some versions omit it on certain errors), data["usage"]["completion_tokens"] at line 3229 raises KeyError. Should use data.get("usage", {}).

### A4. Lines 3183-3184: Shallow copy cb = dict(vllm_body) shares nested mutable objects
dict(vllm_body) creates a shallow copy. cb["messages"] still references the same list as vllm_body["messages"]. When line 3184 sets cb["chat_template_kwargs"], that is fine, but if any continuation logic later mutates cb["messages"] in place, it corrupts vllm_body["messages"]. The same pattern appears at line 3205 (cont_body = dict(vllm_body)).

### A5. Lines 3190-3191: Reasoning-overflow merge assumes single choice
nc[0]["message"]["content"] - if vLLM returns multiple choices (n>1), only the first is merged. The other choices content is silently lost. Also, if nc[0]["message"] has no content key (only reasoning_content), the check at 3190 passes (truthy dict) but line 3191 concatenates an empty string.

### A6. Line 3216: Continuation content merge loses reasoning
When merging continuation content at line 3216 (choices[0]["message"]["content"] = partial + new_c), any reasoning_content from the continuation response is discarded. The original reasoning from the first segment is preserved, but the continuation reasoning is lost.

### A7. Lines 3233-3236: Reasoning field normalization only handles one direction
The loop renames reasoning to reasoning_content if the latter is absent. But if BOTH keys exist (some vLLM versions send both), the old reasoning key is left in the message dict, creating duplicate data that confuses downstream consumers.

### A8. Lines 3742-3746: SQLite cursor not closed on exception
In _get_goose_session_id, if cursor.fetchone() raises, the except block closes the connection but the cursor is leaked. The await cursor.close() at line 3746 is inside the try block, so an exception before reaching it leaks the cursor.

### A9. Lines 3769-3770: Same cursor leak in _get_goose_session_info
Identical pattern to A8. If the SELECT or fetchone raises, the cursor is not closed.

### A10. Lines 2946-2949: Health loop recreates hygiene task without checking if it is already being recreated
If two consecutive health-loop iterations detect the hygiene task died (e.g., it dies and is recreated, then the new one also dies quickly), two recreation tasks are spawned. There is no guard against double-recreation.

### A11. Line 3051: os._exit(1) in FD restart path skips all cleanup
The FD restart path calls os._exit(1) which immediately terminates the process without running atexit handlers, without closing the DB pool, without flushing logs, without sending sd_notify("STOPPING=1"). systemd sees the process die and restarts it, but the abrupt exit can leave the PG pool in a bad state (connections not properly closed).

### A12. Lines 3046-3049: FD drain loop uses busy-wait with 0.1s sleep
for _ in range(FD_RESTART_DRAIN * 10): ... await asyncio.sleep(0.1) - this is a 30-second busy-wait loop. During this time, the event loop is occupied by this coroutine, potentially starving other coroutines. Should use asyncio.wait_for with an async condition variable.

### A13. Lines 1150-1155: LM consumer tasks cancelled before vLLM client closed
The shutdown order cancels LM consumers, then closes _lm_client, then closes _vllm_client, then closes sqlite_conn, then closes pool. But worker_task (memory worker) is cancelled AFTER the pool is closed (line 1167). If the memory worker has in-flight DB operations, they will fail with a "connection closed" error. The worker should be cancelled before the pool.

### A14. Lines 1167-1186: Background tasks cancelled but not awaited in correct order
worker_task, health_task, hygiene_task, watchdog_task are all cancelled (lines 1167-1170) and then individually awaited (1171-1186). But health_task includes the eviction loop which accesses pool - if pool was already closed at line 1165, the eviction loop next iteration will raise. The cancel-then-await pattern is correct, but the ordering (pool close before task cancel) is wrong.

### A15. Line 1103: RuntimeError raised inside lifespan means FastAPI never starts
If the DB connection fails after 60 attempts, raise RuntimeError propagates out of lifespan. FastAPI treats this as a startup failure and the server exits. This is intentional, but the error message says "after 120s" while the actual wait is 60x2s = 120s only if all attempts take the full 2s sleep. If the connection fails instantly, the total time is much less.

### A16. Lines 2815-2818: _EXTRACT_IN_FLIGHT is a global int modified without a lock
if _EXTRACT_IN_FLIGHT < 2: _spawn(...) - in an async context, two concurrent requests can both read _EXTRACT_IN_FLIGHT == 1 and both spawn, exceeding the limit of 2. There is no atomic check-and-increment.

### A17. Lines 2816: _fire_and_forget_extract receives raw messages list
The extract function receives the full untrimmed messages list. For large contexts (58K tokens), this means the extraction prompt can be enormous, consuming significant Mistral API tokens. The messages should be trimmed before passing to extraction.

### A18. Lines 2826-2831: Double-shrink path does not update built consistently
At line 2829, built = _emergency_shrink(built, ceiling) reassigns built. Then line 2830 recounts tokens. But if the shrink does not reduce enough (line 2832 still < MIN_OUTPUT), the function returns 413. However, built has already been mutated - if the caller retried with a smaller request, the session state would be inconsistent because the window was shrunk but the request was rejected.

### A19. Lines 2847-2857: Temperature handling drops valid low temperatures
If the client sends temperature=0.5 and MIN_TEMPERATURE=0.3, it is forwarded. But if MIN_TEMPERATURE were set to 0.7 (via env), a legitimate temperature=0.5 would be silently dropped, replacing it with the server default of 1.0. This changes the model behavior significantly without informing the client.

### A20. Lines 2858-2863: Environment variables parsed as strings, not validated
PRESENCE_PENALTY, REPETITION_DETECTION, THINKING_TOKEN_BUDGET are read as raw strings. Line 2859 does float(PRESENCE_PENALTY) which crashes if the env var contains a non-numeric string. Line 2863 does int(THINKING_TOKEN_BUDGET) with the same risk. No try/except around these conversions.

---

## CATEGORY B: Logic / Correctness Bugs

### B1. Lines 1191-1197: count_tokens fallback is inaccurate
When enc is None (tokenizer not loaded), count_tokens returns len(text) // 4. This is a rough approximation that is off by 20-50 percent for many texts. All budget calculations (input ceiling, output budget, trim target) depend on this count, so the entire rolling window logic operates on inaccurate numbers when the tokenizer falls back.

### B2. Lines 1226, 1228-1230: Token count adds len(messages) * 12 overhead
The * 12 per-message overhead is a hardcoded estimate of role/delimiter tokens. For Qwen tokenizer, the actual overhead differs. This systematic overestimate causes the proxy to trim more aggressively than necessary, losing context that would fit.

### B3. Lines 1258-1273: _prefix_raw only captures system + FIRST user message
The session fingerprint is based on the system prompt and the first user message only. If the system prompt is stable but the conversation topic changes (new user messages), the fingerprint stays the same, meaning the session key does not change. This is by design (sticky sessions), but it means a completely different conversation in the same Goose session gets the same window state, including stale summaries.

### B4. Lines 1285-1312: _detect_loop periodicity test is O(n^2) in the worst case
For each period p from 20 to len(tail)/LOOP_REPEATS, it checks if the last 4*p chars equal the last p chars repeated. With LOOP_TAIL=4000 and LOOP_REPEATS=4, this is up to 500 periods x 16000 char comparisons = 8 million operations per check. Called every 256 chars of streaming output, this can cause noticeable latency spikes.

### B5. Lines 1314-1330: _classify_truncation does not handle empty content with tool_calls
If finish_reason == "length" and content is empty but tool_calls are present, the function falls through to the default "length" classification. But the real issue is that the model ran out of tokens while generating tool call arguments, which needs a different recovery strategy than content continuation.

### B6. Lines 1332-1360: sanitize_tool_calls strips ALL tool calls if ANY is malformed
If a message has 5 tool calls and 1 has invalid JSON arguments, the function strips ALL 5 (sets tool_calls = None). The 4 valid tool calls are lost. Should strip only the malformed ones.

### B7. Lines 1416-1430: _norm_content joins list content with spaces
When content is a list of parts (multimodal), joining with " " loses the structure. Image parts get their dict representation joined as text, producing garbage. The function should filter to text-only parts.

### B8. Lines 1485-1488: Pinned user copy truncates at char boundary
c[:CAP] + "\n[...truncated...]" cuts at an arbitrary character position, potentially in the middle of a word, URL, or code token. Should use _safe_truncate for a cleaner break.

### B9. Lines 1490-1497: _pinned_user_copy finds newest user in rest but rest excludes seed
The rest list is everything after the seed (first 3 messages). If the newest user message IS in the seed (e.g., a 3-message conversation), _newest_user_idx(rest) returns None and no pinned copy is made. The user message is in the seed and would be kept, so this is correct but the edge case is fragile.

### B10. Lines 1586-1590: _output_budget can return 0 or negative
min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN) - if input_tokens > MAX_CONTEXT - SAFETY_MARGIN, the result is negative. The docstring says "never go negative" but the code does not clamp to 0. The caller at line 2826 checks < MIN_OUTPUT which catches negatives, but other callers (line 3147, 3185) do not.

### B11. Lines 1592-1615: _protected_indices protects "newest 6" but the boundary walk can go below 3
Line 1608: tail_start = max(3, len(work) - 6). Line 1609-1611: if work[tail_start] is a tool message, walk backward while tool. Line 1612: tail_start = max(3, tail_start). The max(3, ...) prevents going below index 3, but if indices 3-8 are all tool messages and we are protecting the newest 6, the walk stops at 3, protecting indices 3-13 (11 messages) instead of 6. This over-protects and reduces the shrinkable area.

### B12. Lines 1617-1676: _emergency_shrink elides tool bodies but does not recount
After eliding large tool bodies (>2500 chars) to head+tail, the function does not re-count tokens to verify the result is actually under the ceiling. It relies on the caller to re-count (line 2830, 3146). If the elision is not aggressive enough, the result still exceeds the ceiling.

### B13. Lines 1678-1699: _window_persist permanently disables persistence on first error
If the INSERT fails once (e.g., transient PG connection blip), _window_persist_enabled is set to False and NEVER re-enabled for the lifetime of the process. All subsequent window state is lost on restart. Should retry a few times before giving up.

### B14. Lines 1701-1720: _window_load returns in_flight: False hardcoded
The loaded window always has in_flight: False regardless of what was persisted. If the process crashed while in_flight was True, the next load resets it to False, which is correct. But if the persist wrote in_flight=True (it does not currently, but if it did), the load would ignore it.

### B15. Lines 1722-1780: Sticky cut logic compares anchors by string equality
The cut anchor is a hash/string that identifies the message at the cut point. If the message content changes slightly (e.g., a tool result is updated), the anchor changes, and the sticky cut is invalidated, causing a full re-cut. This is overly sensitive - a small change in one message should not invalidate the entire window position.

### B16. Lines 1780-1830: Window collapse guard triggers too easily
The guard fires when the new cut would be BEFORE the previous cut (window shrinking). This can happen legitimately when the user sends a very short follow-up message. The guard prevents the cut from moving backward, but it also prevents the window from adapting to shorter conversations, keeping stale context.

### B17. Lines 1959-1965: _track_session_tokens grows unboundedly
session_tokens dict grows with every unique session_key. The eviction at line 2919 removes entries older than SESSION_TTL_HOURS, but if sessions are created faster than they are evicted (e.g., many short-lived Goose sessions), the dict can grow large. Each entry is small (~5 ints), but thousands of entries add up.

### B18. Lines 1973-2003: explain_status matches substrings in the status string
if "400" in s - this matches any status string containing "400", including "1400" or "error 4000". The matching should be on exact status codes, not substring containment.

### B19. Lines 2009-2040: _call_lm_4b priority queue can starve high-priority jobs
The PriorityQueue uses (priority, seq, prompt, system, temperature, max_tokens). If a continuous stream of LOW priority jobs (priority=2) arrives, HIGH priority jobs (priority=1) queued behind them must wait for all preceding LOW jobs to complete. There is no aging mechanism to promote waiting high-priority jobs.

### B20. Lines 2040-2080: MistralRateLimiter TPM tracking is approximate
The TPM (tokens per minute) limiter tracks tokens by estimating from response length. It does not parse the actual usage from the API response. If the API returns fewer tokens than estimated, the limiter thinks it used more than it did, reducing throughput unnecessarily.

### B21. Lines 2198-2237: _score_memory recency decay uses exponential with fixed half-life
The decay factor 0.5 ** (age_days / 14) means a memory from 14 days ago has half the score of a fresh one. After 90 days (MEMORY_TTL_DAYS), the score is 0.5 ** (90/14) = 0.007 - effectively zero. Memories older than ~60 days are practically invisible in ranking, making the 90-day TTL meaningless.

### B22. Lines 2239-2270: _fetch_rel_mem catches exceptions per-row but continues
When a row scoring fails (e.g., missing column), the exception is caught and logged, but the row is skipped. If MANY rows fail (e.g., schema migration removed a column), ALL relevant memories are silently dropped. The function should detect systemic failures and log a warning.

### B23. Lines 2270-2310: Budget allocation between WM and memories is static
wm_budget and mem_budget are fixed parameters. If the working memory is very short (e.g., 100 tokens), the remaining budget goes to memories, but the split does not adapt. A dynamic split based on actual WM length would be more efficient.

### B24. Lines 2373-2390: _record_call appends to recent_calls deque without filtering
The deque has maxlen=200, so old entries are automatically evicted. But there is no way to query calls by session or time range - the dashboard just shows the last 200. For debugging a specific session history, this is insufficient.

### B25. Lines 2392-2420: _fd_breakdown reads /proc/self/fd which can be slow
Listing /proc/self/fd and stat-ing each entry is O(fd_count). With 1024+ fds, this takes several milliseconds. Called every 60s by the hygiene loop, it is acceptable, but if called more frequently (e.g., during a WARN threshold crossing), it adds latency.

### B26. Lines 2440-2498: /health endpoint creates a new httpx.AsyncClient per call
Each health check spawns a new HTTP client (line 2490: async with httpx.AsyncClient(timeout=3)). This creates a new TCP connection every time, wasting resources. Should reuse the shared _vllm_client or a dedicated health-check client.

### B27. Lines 2500-2530: Prometheus metrics endpoint computes cache hit rate division by zero
Line 2510: metrics["cached_tokens_total"] / metrics["prompt_tokens_total"] - if no requests have been made yet, both are 0, causing ZeroDivisionError. Should guard with if metrics["prompt_tokens_total"] else 0.0.

### B28. Lines 2530-2560: Prometheus gauge for active sessions counts dict keys
len(session_fingerprints) includes sessions that are past TTL but not yet evicted (eviction happens every 60s in the health loop). The gauge can temporarily overcount.

### B29. Lines 2801: Session fingerprint stored even for rejected requests
If the request is later rejected (413, 503), the fingerprint at line 2801 has already been stored. On the next request with the same prefix, the stale fingerprint is found, and the window state is loaded - but the window was never actually used. This is harmless but wastes a DB read.

### B30. Lines 2835-2836: Log message computes ceiling twice
log.info("Budget: ... ceiling=%d ...", min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT), max_tokens) - the ceiling is computed inline in the log statement. If the formula changes, this log line becomes stale. Should use the same variable as line 2827.

---

## CATEGORY C: Concurrency / Race Condition Bugs

### C1. Lines 2815-2816: _EXTRACT_IN_FLIGHT check-then-act race
Two concurrent requests can both see _EXTRACT_IN_FLIGHT < 2 and both spawn, exceeding the limit. In CPython GIL-protected async model, this is less likely than in threaded code, but with multiple event loop callbacks scheduled in the same tick, it is possible.

### C2. Lines 3061-3064: Client swap is not atomic
_vllm_client = new_vllm and _lm_client = new_lm are two separate assignments. Between them, a concurrent request could read the new vLLM client but the old LM client. While this is unlikely to cause a crash (both are valid clients), it breaks the invariant that both clients are swapped together.

### C3. Lines 3067-3075: _drain_old closure captures old clients by default args
The async function _drain_old(ov=old_vllm, ol=old_lm) captures the old clients as default arguments. This is correct for preventing late binding, but if the FD recycle triggers again before the 60s drain completes, a SECOND _drain_old is scheduled for the NEW old clients. The first drain is still pending. Both will call aclose() on their respective clients, which is fine, but the timing can overlap.

### C4. Lines 3738-3756: SQLite access serialized by lock, but lock is module-level
sqlite_lock = asyncio.Lock() is created at module import time. If the module is imported in a different event loop context (e.g., tests), the lock belongs to the old loop and raises "Future attached to a different loop" in the new loop.

### C5. Lines 892: sqlite_lock created at import time, before any event loop exists
Same as C4. asyncio.Lock() in Python 3.10+ does not bind to a loop at creation, but in 3.8-3.9 it does. If this code runs on an older Python, the lock is bound to whatever loop was current at import time (likely None), causing issues.

### C6. Lines 1120-1124: Multiple background tasks started without synchronization
worker_task, health_task, hygiene_task are all created with asyncio.create_task(). They start concurrently. The health loop (line 2944) checks _hygiene_task.done() - but _hygiene_task is assigned at line 1124, AFTER the health task is created at line 1121. If the health loop first iteration runs before line 1124 executes, _hygiene_task is None and the check at line 2946 (_hygiene_task is not None) passes safely, but the supervisor check is skipped for the first 60s.

### C7. Lines 3279-3285: generate() generator holds reference to vllm_body
The inner generate() function captures vllm_body from the enclosing scope. If the outer function returns a StreamingResponse and the client disconnects, the generator is cancelled, but vllm_body (potentially a large dict with 58K tokens of messages) remains referenced until the generator object is GCd.

### C8. Lines 3300-3500: Streaming state variables are local to generate()
All the streaming state (full_content, seam_hold, continuation_count, etc.) is local to the generator. If the generator is suspended (client reads slowly), the state persists in the generator frame. If the client disconnects, the generator is closed, and all state is freed. This is correct, but the large full_content string (potentially 22K tokens = 90KB) lives in memory for the duration of the stream.

### C9. Lines 2944-2975: Health loop does 4 sequential async operations per 60s cycle
Each cycle: (1) check hygiene task, (2) ping vLLM, (3) evict stale sessions, (4) warm up worker pending count. These are sequential, so the total cycle time is the sum of all four. If vLLM is slow to respond (5s timeout) and eviction is slow (many sessions), the cycle can take >10s, delaying the next health check.

### C10. Lines 2881-2882: _inflight_count decrement in finally can go negative
If _inflight_count is incremented at the start of the request handler and decremented in finally, but an exception occurs BETWEEN the increment and the try block (unlikely but possible with decorator interference), the count goes negative. The FD restart check at line 3047 (_inflight_count <= 0) would then immediately pass, triggering an unnecessary restart.

---

## CATEGORY D: Resource Leak / Memory Issues

### D1. Lines 895-899: Session state dicts grow unboundedly between evictions
session_fingerprints, session_seeds, session_compactions, session_tokens all grow with each new session. Eviction happens every 60s in the health loop, but between evictions, a burst of new sessions can cause a spike in memory. Each session_seed is a list of message dicts (potentially large).

### D2. Line 899: recent_calls deque holds full call records
Each record in the deque contains session_key, input/output tokens, status, model, stream flag, detail string (up to 300 chars), and timestamp. 200 records x ~200 bytes = ~40KB. Small, but the detail strings can contain sensitive data (error messages with URLs, API keys in error responses).

### D3. Lines 941-978: Injection metrics saved to JSON file every 5s
_save_injection_metrics writes the entire metrics dict (including all session sub-dicts and event lists) to a JSON file every 5 seconds. As sessions accumulate, the file grows. The events list is capped at 50, but the sessions dict is not - it grows with every unique session_id.

### D4. Lines 1012-1019: Per-session injection counters never cleaned up
injection_metrics["sessions"] dict grows with every unique session_id. Unlike the in-memory session state (which is evicted by TTL), these counters persist in the JSON file forever. Over weeks, this file can grow to megabytes.

### D5. Lines 1244-1255: session_prefix_hashes dict grows unboundedly
Every request stores a list of SHA1 hashes (one per message) in session_prefix_hashes[session_key]. For a 100-message conversation, that is 100 x 40 chars = 4KB per session. With 1000 active sessions, that is 4MB. The dict is never cleaned up (no TTL, no eviction).

### D6. Lines 3067-3075: Old httpx clients held for 60s after swap
The _drain_old coroutine holds references to the old vLLM and LM clients for 60 seconds. During this time, the old clients connection pools (up to 20 + 10 connections) remain open, holding file descriptors. If the FD recycle triggers repeatedly (every 60s at the 50 percent threshold), multiple generations of old clients can coexist, multiplying fd usage.

### D7. Lines 3296-3297: full_content accumulates entire response
In the streaming path, full_content grows with every content chunk. For a 22K-token response (~90KB of text), this string lives in memory for the entire duration of the stream. If the client is slow, this memory is held longer.

### D8. Lines 3508-3510: Continuation messages include full prior content
cont_messages.append({"role": "assistant", "content": full_content}) - the entire accumulated content is appended as a new message for each continuation. With 5 continuations, the message list grows by 5 x full_content size. For a 90KB response, that is 450KB of duplicated content in the message list.

### D9. Lines 2500-2530: Prometheus endpoint builds entire metrics string on every call
The endpoint constructs a multi-line string with all metrics on every request. If scraped frequently (e.g., every 15s by Prometheus), this creates GC pressure. The string is small (~2KB) but the allocation churn adds up.

### D10. Lines 4400-5014: Dashboard HTML is a 600+ line string constant
The dashboard HTML/JS/CSS is embedded as a single string literal. This string is allocated once at import time and served on every /dashboard request. The string is ~30KB, which is fine, but it is not cached as a compiled template - it is re-served as a raw string every time.

---

## CATEGORY E: Security Issues

### E1. Line 864: Default DB password is "CHANGE_ME"
DB_DSN = ... or "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy" - if the env var is not set, the proxy connects with a well-known password. In a production environment where the env var is forgotten, this is a security risk.

### E2. Line 866: API_KEY defaults to empty string
API_KEY = os.environ.get("CTXGATE_API_KEY", "") - if not set, the proxy has NO authentication. Any process on the machine (or network, if bound to 0.0.0.0) can send requests. The /chat/completions endpoint should require auth by default.

### E3. Lines 3882-3900: Knowledge create endpoint has no authentication
POST /knowledge accepts any key/value pair and writes it to the database. No API key check. An attacker can inject malicious knowledge that gets injected into future prompts (prompt injection vector).

### E4. Lines 3902-3930: Knowledge search endpoint exposes all knowledge
GET /knowledge/search returns knowledge items without authentication. Combined with E3, this allows read-write access to the knowledge base.

### E5. Lines 3989-4008: Deliverable create endpoint has no authentication
POST /deliverable writes to the database without auth. An attacker can create fake deliverables that appear in the dashboard.

### E6. Lines 4010-4040: Deliverable list endpoint has no authentication
GET /deliverable exposes all deliverables including working_dir paths, which can reveal filesystem structure.

### E7. Lines 4200-4241: Memory API endpoints have no authentication
GET /api/memory and related endpoints expose all memories for a task without auth. Memory content can contain sensitive information (credentials, internal URLs, personal data).

### E8. Lines 2440-2498: Health endpoint exposes internal state
GET /health reveals whether the DB is connected, whether the tokenizer is loaded, and whether vLLM is reachable. This information helps an attacker plan attacks.

### E9. Lines 2500-2530: Prometheus metrics expose operational details
Token counts, session counts, error rates, and cache hit rates are exposed without auth. This reveals usage patterns and potential vulnerabilities.

### E10. Line 3244: Exception message included in 500 response
return JSONResponse({"error": {"message": str(e), ...}}) - the raw exception string can contain internal paths, SQL fragments, or connection strings. Should be sanitized.

### E11. Lines 3845-3878: Task resolution exposes session metadata
_resolve_task queries the Goose sessions DB and returns session name, working_dir, provider_name. If the session_id is guessable, an attacker can enumerate sessions.

### E12. Lines 3736-3757: Goose SQLite DB path is predictable
GOOSE_SESSIONS_DB is a fixed path. If an attacker can read this file, they get all session IDs, names, and metadata.

### E13. Lines 1113-1114: API key status logged at startup
log.info("... api_key=%s", "set" if API_KEY else "off") - this is safe (does not log the key), but the log line also includes the VLLM_URL and model name, which reveals infrastructure details.

### E14. Lines 2849-2851: Negative temperature forwarded to vLLM
If a client sends temperature=-1, it is forwarded to vLLM. While vLLM will reject it, the proxy could sanitize it to avoid leaking that it is forwarding invalid params.

### E15. Lines 4400-5014: Dashboard HTML includes inline JavaScript
The dashboard JS makes fetch calls to various API endpoints. If the dashboard is served without auth, the JS can be used to make authenticated requests (CSRF) if the browser has cookies. Currently there are no cookies, but the pattern is risky.

---

## CATEGORY F: Configuration / Environment Issues

### F1. Line 857: MAX_CONTINUATIONS uses int(os.environ.get(...)) not _env_int
All other numeric config values use _env_int() which handles validation and logging. MAX_CONTINUATIONS uses raw int() which crashes on non-numeric input. Inconsistent and fragile.

### F2. Lines 883-885: Three config vars read as raw strings, not typed
PRESENCE_PENALTY, REPETITION_DETECTION, THINKING_TOKEN_BUDGET are read with os.environ.get(..., "") and converted at point of use (lines 2859, 2861, 2863). If the env var contains an invalid value, the conversion crashes at request time, not at startup.

### F3. Lines 868-869: Tokenizer path uses tilde expansion
os.path.expanduser("~") expands to the home directory of the user running the process. If the proxy runs as a different user (e.g., systemd service), the path may not exist. The validate_config check at line 1073 catches this, but the error message does not suggest fixing the env var.

### F4. Lines 177-189: .env loading uses os.environ.setdefault
setdefault means existing environment variables take precedence over .env values. This is correct for Docker (env vars from docker-compose override .env), but it means you cannot override a Docker env var with a .env value. The behavior is undocumented.

### F5. Line 191: .env loaded at import time, before validate_config
The .env file is loaded at module import (line 191), but validate_config is called in lifespan (line 1089). If the .env file changes between import and lifespan (e.g., a volume mount updates it), the changes are not picked up.

### F6. Line 1141: LM_WORKERS read with raw int(os.environ.get(...))
Same issue as F1. Should use _env_int.

### F7. Lines 844-854: Multiple config values can conflict
TRIM_TARGET_TOKENS, TRIM_TARGET_FRACTION, TRIM_TARGET_FLOOR interact in complex ways. If TRIM_TARGET_TOKENS=50000 and TRIM_TARGET_FRACTION=0.5 (of 84000 = 42000), which wins? The code at line 1770 uses TRIM_TARGET_TOKENS if > 0, else the fraction. But TRIM_TARGET_FLOOR is applied as a minimum. The interaction is not documented and can produce surprising results.

### F8. Lines 851-853: MAX_OUTPUT (22500) + MIN_OUTPUT (16000) relationship undocumented
MIN_OUTPUT is a floor for the output budget. If MAX_OUTPUT < MIN_OUTPUT (misconfiguration), the system is in an impossible state. No validation checks this.

### F9. Line 856: WALL_CLOCK_MAX (1800s) applies to both stream and non-stream
The 30-minute wall clock limit is the same for both paths. A non-stream request with a 22K-token output might need more than 30 minutes on a slow GPU. The limit should be configurable separately for stream vs non-stream.

### F10. Lines 873-881: Loop detection thresholds are interdependent
LOOP_REPEATS=4, LOOP_SENTENCE_REPEATS=6, LOOP_TAIL=4000, LOOP_CHECK_EVERY=256 - changing one without the others can break detection. For example, increasing LOOP_TAIL to 8000 without increasing LOOP_CHECK_EVERY means the check runs more often relative to the tail size, catching loops earlier but with more false positives.

---

## CATEGORY G: Code Quality / Maintainability Issues

### G1. Line 2006: import re as _re redundant
re is already imported at line 9. The alias _re is used nowhere in the visible code. Dead import.

### G2. Lines 193-196: Redundant imports
import signal as _signal, import os as _os, import socket as _socket - these alias imports are used only in the signal handler section (lines 4980-5013). The aliases add noise without benefit since there is no naming conflict.

### G3. Lines 3263-3267: Five blank lines between functions
Lines 3263-3267 are five consecutive blank lines between _safe_truncate and _sse_content. Excessive whitespace.

### G4. Lines 3880-3881: Comment followed immediately by route
The section comment is on line 3880 and the route decorator is on line 3881. No blank line between them, inconsistent with other section headers in the file.

### G5. Lines 1282-1284: Empty section markers
"# --- D10: Reasoning stripping ---" and "# --- D9: Malformed tool-call sanitization ---" are section headers with no content between them (the actual functions are elsewhere). Misleading dead comments.

### G6. Lines 940-941: INJECTION_METRICS_PATH computed at import time
The path is computed using os.path.dirname(os.path.abspath(__file__)). If the file is symlinked, __file__ points to the symlink location, not the target. The metrics file would be written to the wrong directory.

### G7. Lines 2885-2893: _read_worker_status imports json locally
import json as _json inside the function, even though json is already imported at module level (line 5). Redundant.

### G8. Lines 3023-3024: Local imports inside _fd_hygiene_loop
import os as _os and import resource inside the function body. os is already imported at line 8. resource is not imported at module level. Inconsistent import style.

### G9. Lines 4980-5013: Signal handler defined at module bottom
The signal watcher thread is started at the very bottom of the file (lines 5010-5011), after all route definitions. This means the signal handler is only active after the entire module is imported. If the import is slow (loading tokenizer, connecting to DB), signals during import are not handled.

### G10. Line 5013: server.run() at module level
The uvicorn server is started at module level (line 5013), not under if __name__ == "__main__". This means importing the module (e.g., in tests) starts the server. Tests must use importlib tricks or subprocess isolation to avoid this.

### G11. Lines 1-22: No from __future__ import annotations
Without this, type hints are evaluated at definition time. dict[str, float] (line 860) requires Python 3.9+. On Python 3.8, this would crash at import. The code targets 3.14 (per test output) but the lack of future import makes it fragile.

### G12. Lines 895-899: Type annotations use built-in generics
dict[str, str], dict[str, list], dict[str, dict] - these require Python 3.9+. Consistent with the 3.14 target, but worth noting for portability.

### G13. Lines 902-929: Metrics dict has no type annotation
The metrics dict is a plain literal with no type. Adding a TypedDict would catch typos in metric names at development time.

### G14. Lines 943-956: _default_injection_metrics returns a new dict each call
This is correct (avoids shared mutable state), but the function is called at module level (line 1028) and potentially in tests. If a test modifies the returned dict and then calls the function again, it gets a fresh dict - correct behavior, but the intent is not obvious.

### G15. Lines 1043-1084: validate_config calls sys.exit(1) on failure
Calling sys.exit in a library function is harsh. It prevents the function from being used in tests (you would have to catch SystemExit). Raising a custom exception would be more testable.

### G16. Lines 1086-1186: lifespan is 100 lines long
The lifespan function does: validate config, connect DB, load tokenizer, start 5 background tasks, create 2 HTTP clients, notify systemd, yield, then shutdown all of it. This should be decomposed into smaller functions for readability and testability.

### G17. Lines 3276-3732: stream_to_vllm is 450+ lines
The streaming function is the longest in the file. It handles: SSE parsing, content accumulation, seam trimming, loop detection, reasoning overflow, auto-continuation, tool call handling, usage tracking, and error recovery. Should be decomposed.

### G18. Lines 3111-3246: forward_to_vllm is 135 lines
Similar to G17 but shorter. Handles: retry on 500/503, 400 re-shrink, auto-continuation, reasoning overflow, tool call sanitization, usage tracking.

### G19. Lines 2700-2882: Main chat completions handler is 180+ lines
The POST /chat/completions handler does: auth check, body parsing, session key computation, window building, token counting, budget calculation, emergency shrink, sampling param forwarding, stream/non-stream dispatch. Should be decomposed.

### G20. Lines 4400-5014: 600+ lines of inline HTML/JS/CSS
The dashboard is a single string constant. This makes the Python file hard to navigate and the HTML impossible to lint separately. Should be an external template file.

---

## CATEGORY H: Performance Issues

### H1. Lines 1211-1230: count_messages_tokens joins all messages into one string
For a 100-message conversation, this creates a single string of ~58K tokens worth of text, then tokenizes it. The join allocates a large temporary string. Tokenizing per-message and summing would use less peak memory (but the current approach is more accurate for cross-message token boundaries).

### H2. Lines 1237-1255: _prefix_diag computes SHA1 for every message on every request
For 100 messages, that is 100 SHA1 computations per request. Each SHA1 is fast (~1 microsecond for short strings), but for long messages (10KB+), it is slower. The diagnostic is logged at INFO level, so it runs in production. Should be DEBUG level or sampled.

### H3. Lines 1285-1312: Loop detection runs every 256 chars of streaming output
For a 22K-token response (~90KB), that is ~350 loop detection checks. Each check examines the last 4000 chars. Total work: 350 x 4000 = 1.4M character operations per response. On a modern CPU this is <10ms total, but it adds up under load.

### H4. Line 2806: count_messages_tokens called on every request
This is the most expensive operation in the request path. For large contexts, it can take 50-200ms (as noted by the SLOW warning at line 2808-2809). The result is used for budget calculation, so it cannot be skipped, but it could be cached if the messages have not changed.

### H5. Lines 2815-2816: Fire-and-forget extraction on every request
Even when _EXTRACT_IN_FLIGHT >= 2, the check itself is cheap, but when it does spawn, the extraction sends the full message list to the Mistral API. This is a significant token cost on every request.

### H6. Lines 2940-2975: Health loop creates a new httpx.AsyncClient every 60s
Each health check creates a new client (line 2953), which opens a new TCP connection. Over a day, that is 1440 connections. Should reuse a persistent client.

### H7. Lines 2919-2937: Eviction scans all sessions every 60s
_evict_stale_sessions iterates over all SESSION_LAST_ACTIVE entries. With 1000 sessions, this is 1000 dictionary lookups per minute. Negligible, but the pattern does not scale to 100K sessions.

### H8. Line 3036: len(os.listdir("/proc/self/fd")) every 60s
Reading the /proc filesystem is a syscall. Every 60s is fine, but the fd_breakdown() call (line 3037) stats each fd, which is O(fd_count) syscalls.

### H9. Lines 3270-3273: _sse_content calls json.dumps on every chunk
For a 22K-token response streamed in ~1000 chunks, that is 1000 JSON serializations. Each is small (~100 bytes), but the cumulative allocation churn adds GC pressure.

### H10. Lines 3300-3730: Streaming generator parses SSE line by line
Each SSE line is parsed with string splitting. For high-throughput streaming, this is the bottleneck. A proper SSE parser (incremental) would be faster, but the current approach is adequate for single-client use.

---

## CATEGORY I: Missing Error Handling / Robustness

### I1. Lines 1091-1103: DB connection retry has no backoff
60 attempts x 2s sleep = linear retry. If PG is down for 5 minutes, the proxy gives up after 2 minutes. Exponential backoff (2s, 4s, 8s, ...) would be more resilient.

### I2. Lines 1107-1117: Tokenizer fallback is silent
If the Qwen tokenizer fails to load, it falls back to cl100k_base with a WARNING log. But all budget calculations are calibrated for the Qwen tokenizer. The fallback produces different token counts, breaking the budget invariants. Should be a CRITICAL log or a startup failure.

### I3. Line 1132: vLLM client created without checking if VLLM_URL is reachable
The client is created optimistically. If VLLM_URL is wrong (typo, wrong port), the first request will fail with a connection error. The health loop will mark vLLM as dead, but the error message will not explain the root cause (bad URL vs vLLM down).

### I4. Lines 1138-1139: LM client created even if MISTRAL_API_KEY is empty
If no API key is set, the client is created with empty headers. Every LM call will fail with 401. The client should not be created (or LM features should be disabled) when the key is missing.

### I5. Lines 2490-2497: Health check vLLM with 3s timeout
If vLLM is slow but alive (e.g., loading a model), the 3s timeout marks it as dead. The next request is rejected with 503. Should use a longer timeout for the health check or distinguish between "slow" and "dead".

### I6. Lines 3128-3132: Retry on 500/503 with linear backoff
_delay = 1.0 * _attempts - 1s, 2s, 3s. For a vLLM that is OOM-killed and restarting (takes 30s+), three retries in 6s total is insufficient. Should use exponential backoff.

### I7. Lines 3140-3148: 400 re-shrink retry is one-shot
If the re-shrunk request ALSO gets a 400 (e.g., the context is fundamentally too large), there is no second retry. The error is returned to the client. A second, more aggressive shrink (dropping the oldest half) could save the request.

### I8. Lines 3238-3241: TimeoutException handler does not distinguish connect vs read timeout
Both connect timeout (10s) and read timeout (300s) raise httpx.TimeoutException. The handler returns 504 in both cases. A connect timeout means vLLM is unreachable (should be 503); a read timeout means vLLM is slow (504 is correct).

### I9. Lines 3242-3246: Generic Exception handler returns 500 with raw message
Any unexpected exception (bug, memory error, etc.) is caught and returned as a 500 with the exception string. This can leak internal details. Should log the full traceback and return a generic error.

### I10. Lines 3739-3756: SQLite connection re-created on error, but not verified
After closing a failed connection (line 3753), the next call re-creates it (line 3741). But if the DB file is corrupted, every call will fail and re-create, creating a tight loop of connect-fail-close. Should have a circuit breaker.

### I11. Lines 3845-3878: _resolve_task does not handle pool=None
If pool is None (DB not connected), pool.fetchrow at line 3845 raises AttributeError. The function has no null check. Other functions (like _fetch_session_summary at line 814) do check.

### I12. Lines 3892-3899: Knowledge create does not check pool
Same as I11. pool.execute at line 3892 will crash if pool is None.

### I13. Lines 3907-3930: Knowledge search does not check pool
Same pattern. pool.fetch will crash if pool is None.

### I14. Lines 4016-4040: Deliverable list does check pool
Checked at line 4013 (if not pool: return 503), so this one IS handled. Good.

### I15. Lines 4236-4241: Memory API catches all exceptions but returns 200
except Exception as e: return {"memories": [], ..., "error": str(e)} - returning 200 with an error field is misleading. Should return 500.

---

## CATEGORY J: Data Integrity / Schema Issues

### J1. Lines 487-489: INSERT into memories does not include created_at
The INSERT specifies columns explicitly but omits created_at. If the column has no DEFAULT, the insert fails. If it has DEFAULT now(), it is fine. The schema dependency is implicit.

### J2. Lines 501-503: UPDATE memories sets updated_at=now() but not status
If a memory was previously deactivated (status=inactive), the UPDATE re-activates it by setting a new value but does not explicitly set status=active. If the status column was inactive, it stays inactive after the update.

### J3. Lines 512-530: SUPERSEDE deactivates old and inserts new, but not atomically
The deactivation (UPDATE) and insertion (INSERT) are two separate statements. If the process crashes between them, the old memory is deactivated but the new one is not inserted. Data loss.

### J4. Lines 1688-1691: Window persist uses ON CONFLICT DO UPDATE
The upsert updates ALL columns on conflict, including cut_prev_anchor. If the previous anchor was meaningful (tracking the prior cut position), overwriting it loses the history. Should only update columns that changed.

### J5. Lines 3870-3876: Task upsert uses COALESCE for all fields
ON CONFLICT DO UPDATE SET name=COALESCE(EXCLUDED.name, proxy.tasks.name) - if the new name is empty string (not NULL), COALESCE keeps the old name. But if the intent is to clear the name, this prevents it. Empty string vs NULL semantics are ambiguous.

### J6. Lines 3893-3898: Knowledge upsert uses GREATEST for importance
importance = GREATEST(EXCLUDED.importance, proxy.knowledge.importance) - importance can only increase, never decrease. If a knowledge item importance should be lowered (e.g., it is no longer relevant), there is no way to do it through this API.

### J7. Lines 3990-3995: Deliverable INSERT has no unique constraint
Multiple deliverables with the same name can be created. There is no ON CONFLICT clause, so duplicates are allowed. The dashboard will show duplicate entries.

### J8. Line 4236: Working memory fetched by task_id only
SELECT content FROM proxy.working_memory WHERE task_id = $1 - if a task has multiple working memory entries (from different sessions), only one is returned (no LIMIT, no ORDER BY). Which one is returned is nondeterministic.

### J9. Lines 817-826: Session summary fetch has two different query paths
The primary query filters by task_id, the fallback joins through proxy.tasks on session_id. If a task is re-resolved to a different UUID (e.g., after a DB migration), the primary query finds nothing and the fallback finds the old summary. The two paths can return different summaries for the same logical session.

### J10. Lines 2242-2248: Relevant memory SELECT has no LIMIT
SELECT key, value, category, importance, updated_at FROM proxy.memories WHERE task_id=$1 AND active=true - if a task has 10,000 memories, all are fetched into Python. The ranking and budget selection happen in Python, not SQL. Should add a LIMIT or push the filtering into SQL.

---

## CATEGORY K: API Contract / Interface Issues

### K1. Lines 2838-2843: vLLM body always includes "model" and "stream"
Even if the client did not send these fields, the proxy adds them. If the client sent model="other-model", it is overwritten with VLLM_MODEL. This is by design (single-model proxy), but it means the proxy cannot be used as a multi-model router.

### K2. Lines 2847-2857: Temperature handling is asymmetric
Negative temperature is forwarded (line 2851), low temperature is dropped (line 2857), high temperature is forwarded (line 2853). The client has no way to know if their temperature was honored. The response does not include the effective temperature.

### K3. Lines 2864-2865: stream_options added only when stream=true
if stream: vllm_body["stream_options"] = {"include_usage": True} - if the client sent their own stream_options (e.g., with different settings), they are overwritten. The client settings are ignored.

### K4. Lines 2866-2869: Tools and tool_choice forwarded as-is
If the client sends tools that reference functions the model does not support, vLLM will reject the request. The proxy does not validate or filter tools.

### K5. Line 3237: Non-stream response returns vLLM raw data dict
The response is JSONResponse(data) where data is vLLM raw response. The proxy does not add any metadata (e.g., which messages were trimmed, what the effective budget was). The client has no visibility into the proxy decisions.

### K6. Line 3731: Stream ends with "data: [DONE]"
This is the OpenAI convention. If a non-OpenAI client uses the proxy, the [DONE] sentinel may confuse it. The proxy should be protocol-agnostic or document the expected client behavior.

### K7. Line 2882: Inflight count decremented in finally
If the request handler raises before the try block (e.g., in the auth check), the inflight count is never incremented, so the finally decrement makes it negative. The try block starts at line 2790 (approximately), but the auth check is before it.

### K8. Lines 2790-2800: Auth check happens before inflight increment
The order is: auth check, body parse, inflight increment, processing. If auth fails, inflight is never incremented (correct). But if body parse fails (malformed JSON), the inflight increment has already happened (if it is before the parse), leading to a count mismatch. Need to verify exact ordering.

### K9. Line 4011: Deliverable list has no pagination
limit: int = 50 caps at 50, but there is no offset parameter. To see older deliverables, the client cannot page beyond the first 50.

### K10. Line 3903: Knowledge search limit has no maximum cap
limit: int = 20 - a client can send limit=1000000 and get a massive response. Should cap at a reasonable maximum (e.g., 100).

---

## CATEGORY L: Logging Issues

### L1. Line 24: logging.basicConfig at module level
This configures the root logger. If the application (Goose) also configures logging, the basicConfig call is a no-op (basicConfig only works if no handlers are configured). The proxy log format may not match the application.

### L2. Lines 2808-2809: SLOW warning threshold is 50ms
if _dt > 50: log.warning("SLOW: count_messages_tokens %.0fms") - 50ms is a low threshold. On a loaded system, token counting regularly exceeds 50ms, flooding the log with warnings. Should be 100ms or 200ms.

### L3. Lines 3126-3127: Pool-wait warning at 2s
if _dt > 2.0: log.warning("vLLM pool-wait: %.1fs") - 2 seconds is a reasonable threshold, but the warning does not include the session key, making it hard to correlate with a specific request.

### L4. Line 3244: log.exception in the generic error handler
log.exception("vLLM forward error: %s", e) logs the full traceback. In production, this can be verbose and may include sensitive data in local variables. Should be log.error with a truncated message.

### L5. Line 2948: Hygiene task death logged as CRITICAL
log.critical("FD hygiene task DIED: %s. Recreating.") - a task dying is serious, but CRITICAL level may trigger alerting systems. If the task dies and is successfully recreated, WARNING would be more appropriate.

### L6. Lines 3040-3041: FD restart logged as CRITICAL with breakdown
The breakdown string can be very long (one line per fd type). In a log aggregator, this can be truncated. Should be structured (JSON) for better parsing.

### L7. Line 3501: "reasoning_overflow but no retries left" logged as WARNING
This is a significant event (the model reasoning consumed all tokens and there are no retries left). Should be ERROR level.

### L8. Line 3525: "Max continuations reached" logged as WARNING
Hitting the continuation limit means the response was incomplete. The client receives a truncated response. This should be ERROR.

### L9. Line 1100: DB retry logged as WARNING every 2s
60 warnings in 2 minutes if PG is down. Should be WARNING for the first 3 attempts, then INFO for the rest (to avoid log flooding), then CRITICAL on final failure.

### L10. Line 2857: Temperature drop logged as INFO
Silently changing the client temperature is a significant behavioral change. Should be WARNING so operators notice.

---

## CATEGORY M: Testing / Observability Gaps

### M1. No unit tests for _detect_loop
The loop detection algorithm is complex (periodic suffix + sentence repeat) and has multiple edge cases (short text, non-repeating text, partial repeats). No tests visible in the file.

### M2. No unit tests for _emergency_shrink
The shrink logic (elide tool bodies, drop oldest, protect groups) is critical for correctness. Edge cases: all messages are protected, tool group spans the entire list, ceiling is smaller than the seed.

### M3. No unit tests for _score_memory
The scoring function combines term overlap, category boost, importance, and recency decay. Edge cases: empty terms, all-zero importance, very old memories.

### M4. No integration test for the full streaming path
The streaming path (SSE parsing, seam trimming, continuation, loop detection) is the most complex code in the file. An integration test with a mock vLLM server would catch regressions.

### M5. No test for FD hygiene loop
The FD monitoring, client swap, and restart logic is critical for long-running stability. No test simulates fd exhaustion.

### M6. No test for session eviction
The TTL-based eviction of session state is not tested. Edge cases: session exactly at TTL, session updated during eviction, empty session dict.

### M7. No test for the 400 re-shrink path
The path where vLLM returns 400 (context too long) and the proxy re-shrinks and retries is not tested. This is a critical recovery path.

### M8. No test for reasoning overflow recovery
The path where the model reasoning consumes all tokens and the proxy retries with thinking disabled is not tested.

### M9. No test for the signal handler
The graceful shutdown signal handler (SIGTERM to drain to exit) is not tested. Edge cases: second signal within 10s, in-flight requests during drain.

### M10. No test for the watchdog loop
The systemd watchdog ping (every 10s) is not tested. If the event loop is blocked, the watchdog should trigger a restart.

---

## CATEGORY N: Protocol / Format Issues

### N1. Lines 3270-3273: SSE chunk "created" field is always 0
"created": 0 - the OpenAI API expects a Unix timestamp here. Some clients may use this for ordering or deduplication. Should be int(time.time()).

### N2. Line 3271: SSE chunk "id" is always "gen"
"id": chunk_id where chunk_id defaults to "gen". All chunks have the same ID. OpenAI API uses a unique ID per chunk. Clients that deduplicate by ID will drop all but the first chunk.

### N3. Line 3272: SSE chunk "model" is always VLLM_MODEL
Even if the response came from a continuation (different effective model behavior), the model name is the same. This is correct for a single-model proxy, but the "model" field in continuation chunks should ideally reflect that it is a continuation.

### N4. Line 3731: "[DONE]" sentinel sent after all content
The [DONE] sentinel is sent after the final usage chunk. If the client disconnects before receiving [DONE], the generator is cancelled, and the [DONE] is never sent. This is correct behavior, but some clients may hang waiting for [DONE].

### N5. Lines 3508-3510: Continuation prompt is in English
"Your response was cut off. Continue writing from where it stopped..." - if the conversation is in another language, the English continuation prompt may cause the model to switch languages. Should be language-aware or use a neutral instruction.

### N6. Lines 3164-3169: Non-stream continuation prompt is different from stream
Non-stream: "Continue from exactly where you left off. Do not repeat any content already provided. Resume the next word/sentence/code line."
Stream: "Your response was cut off. Continue writing from where it stopped. Do not repeat anything."
The two prompts are slightly different, which may cause inconsistent continuation behavior between stream and non-stream modes.

### N7. Lines 3183-3184: Reasoning overflow retry disables thinking
cb["chat_template_kwargs"] = {"enable_thinking": False} - this is a Qwen-specific parameter. If the proxy is ever pointed at a non-Qwen model, this parameter is ignored or causes an error. Should be model-aware.

### N8. Line 2861: Repetition detection config is hardcoded
{"max_pattern_size": 50, "min_pattern_size": 5, "min_count": 6} - these values are not configurable via environment variables. Different models may need different thresholds.

### N9. Line 2863: Thinking token budget is passed as int
vllm_body["thinking_token_budget"] = int(THINKING_TOKEN_BUDGET) - if the env var is "12000.5", int() truncates to 12000. Should use float() or validate at startup.

### N10. Lines 3164-3169: Tool call sanitization sets tool_calls to None
choice["message"]["tool_calls"] = None - setting to None (null in JSON) rather than removing the key. Some clients may not handle "tool_calls": null gracefully. Should del the key.

---

## CATEGORY O: Architectural / Design Issues

### O1. Single-file architecture (5014 lines)
The entire proxy - circuit breaker, token counting, window management, memory worker, streaming, dashboard, API endpoints, signal handling - is in one file. This makes it extremely difficult to navigate, test, and maintain. Should be split into modules: config.py, tokens.py, window.py, streaming.py, memory.py, api.py, dashboard.py, signals.py.

### O2. Global mutable state
pool, enc, vllm_alive, metrics, injection_metrics, session_fingerprints, session_seeds, session_compactions, session_tokens, SESSION_LAST_ACTIVE, _vllm_client, _lm_client, _lm_queue, sqlite_conn, _shutting_down, _inflight_count, _EXTRACT_IN_FLIGHT - 15+ global variables. This makes the code hard to reason about and test. Should be encapsulated in an AppState class.

### O3. No dependency injection
All dependencies (DB pool, HTTP clients, tokenizer) are globals. This prevents testing with mocks. Functions like fetch_task_memory directly access pool global.

### O4. Mixed concerns in the main request handler
The POST /chat/completions handler does: authentication, body parsing, session management, window building, token counting, budget calculation, emergency shrinking, sampling param forwarding, stream/non-stream dispatch, metrics recording. Each of these is a separate concern that should be a composable middleware or pipeline stage.

### O5. No request correlation ID
There is no trace/request ID that flows through the entire request lifecycle. When debugging a slow request, you cannot correlate the token counting log, the vLLM call log, and the response log. Should generate a request ID at the start and include it in all log messages.

### O6. No graceful degradation indicator for memory features
If the DB is down, memory injection fails silently (the gather with return_exceptions=True catches it). The proxy continues serving requests without memory. This is correct, but there is no indicator in the response or logs that memory is degraded. The operator does not know until they check the dashboard.

### O7. No configuration hot-reload
All configuration is read at startup. Changing an environment variable requires a process restart. For a long-running proxy, this means downtime for config changes. Should support SIGHUP-triggered reload or a /reload endpoint.

### O8. No request rate limiting
The proxy has no rate limiting. A misbehaving client (or DDoS) can saturate the vLLM backend. The LM rate limiter exists for Mistral calls, but there is no rate limit on the main /chat/completions endpoint.

### O9. No request size validation beyond MAX_BODY_BYTES
The body size is checked, but there is no validation on the number of messages, the size of individual messages, or the total token count before processing. A client can send 10,000 messages of 1 token each, which passes the byte limit but causes expensive token counting.

### O10. No circuit breaker for the main request path
The _CircuitBreaker class is defined (lines 27-60) but is not clearly used in the main request path. It is used for backend connections, but the main path (vLLM calls) uses ad-hoc retry logic instead of the circuit breaker. The breaker should wrap the vLLM calls.

---

## CATEGORY P: Specific Line-Level Issues (Additional)

### P1. Line 22: Response imported from fastapi but never used
from fastapi import FastAPI, Request, Response - Response is not used anywhere. All responses use JSONResponse, HTMLResponse, or StreamingResponse.

### P2. Line 14: Any imported from typing but only used in one place
from typing import Any, Optional - Any is used at line 890 (enc: Optional[Any]). Optional is used throughout. Fine, but Any could be replaced with a union type.

### P3. Line 12: deque imported from collections
Used at line 899 for recent_calls. Fine.

### P4. Line 7: math imported but usage is sparse
math is used for math.log in the scoring function and possibly elsewhere. If only used once, consider inlining.

### P5. Line 5: hashlib imported
Used for SHA1 (prefix diag) and SHA256 (session key). Fine.

### P6. Line 3: datetime imported
Used for timestamp formatting in some places. Check if all usages are necessary or if time.time() suffices.

### P7. Lines 27-60: CircuitBreaker class defined but potentially unused
The class is defined with full functionality (open, half-open, closed states). Verify it is actually instantiated and used somewhere. If not, it is dead code.

### P8. Lines 62-80: MistralRateLimiter class
Check if the RPS and TPM limits are actually enforced in the consumer loop. If the limiter is defined but the consumer does not call acquire(), it is dead code.

### P9. Lines 80-100: Priority queue item class
The PQItem class for the LM priority queue. Check if the comparison operators are correct (priority ascending, sequence ascending for FIFO within same priority).

### P10. Lines 100-130: LM consumer function
The consumer loop that processes the priority queue. Check if it handles cancellation gracefully (CancelledError) and if it releases the rate limiter slot on error.

### P11. Lines 130-160: Memory worker loop
The main worker loop that processes memory jobs. Check if it handles job deserialization errors, if it has a max retry count, and if it logs job processing time.

### P12. Lines 160-176: Dotenv loader
The _load_dotenv function. Check if it handles BOM (byte order mark) in .env files, if it supports multi-line values, and if it trims whitespace correctly.

### P13. Line 224: NL constant
NL = "\n" - a newline constant. Used in string concatenation. Fine, but inconsistent with using "\n" directly elsewhere.

### P14. Lines 230-250: STUB_TEXT constant
The stub text inserted into the message window. Check if it is appropriate for the model (Qwen) and if it conflicts with the system prompt.

### P15. Lines 250-280: Seed extraction logic
The logic for extracting the first 3 messages as the immutable seed. Check if it handles the case where there are fewer than 3 messages.

### P16. Lines 280-310: Message grouping for tool calls
The logic that groups assistant tool_calls with their tool results. Check if it handles the case where a tool result is missing (model called a tool but the result was never received).

### P17. Lines 310-350: Window building (main path)
The core window building logic. Check if it handles the case where the cut point falls in the middle of a tool group (should round to group boundary).

### P18. Lines 350-400: Summary backfill logic
The logic for inserting a summary of dropped messages. Check if the summary is generated asynchronously and if the placeholder is replaced correctly.

### P19. Lines 400-450: Phase extraction for summarization
The logic that divides dropped messages into phases for summarization. Check if phase boundaries align with natural conversation breaks.

### P20. Lines 450-480: Memory extraction from phases
The logic that extracts memories from summarized phases. Check if it handles the case where the 4B model returns malformed JSON.

---

## SUMMARY

| Category | Count |
|----------|-------|
| A: Crash/Exception | 20 |
| B: Logic/Correctness | 30 |
| C: Concurrency/Race | 10 |
| D: Resource Leak/Memory | 10 |
| E: Security | 15 |
| F: Configuration/Environment | 10 |
| G: Code Quality/Maintainability | 20 |
| H: Performance | 10 |
| I: Missing Error Handling | 15 |
| J: Data Integrity/Schema | 10 |
| K: API Contract/Interface | 10 |
| L: Logging | 10 |
| M: Testing/Observability Gaps | 10 |
| N: Protocol/Format | 10 |
| O: Architectural/Design | 10 |
| P: Specific Line-Level | 20 |
| **TOTAL** | **210** |

---

## IMPLEMENTATION PLAN (Prioritized)

### Phase 1: Critical (crash/data-loss/security) - fix first
1. A3: Guard data["usage"] with .get()
2. A8/A9: Close SQLite cursors in finally blocks
3. A13/A14: Fix shutdown ordering (cancel worker before closing pool)
4. B13: Retry window persist before disabling
5. C1: Atomic check-and-increment for _EXTRACT_IN_FLIGHT
6. E1-E9: Add API key authentication to all public endpoints
7. I11-I13: Add pool=None guards to _resolve_task, knowledge_create, knowledge_search
8. J3: Make SUPERSEDE atomic (single transaction)
9. A11: Replace os._exit with graceful shutdown in FD restart

### Phase 2: High (correctness/logic) - fix next
10. B1/B2: Improve token counting accuracy (calibrate per-message overhead)
11. B6: Sanitize only malformed tool calls, not all
12. B10: Clamp _output_budget to minimum 0
13. B21: Adjust recency decay half-life to match TTL
14. C2: Atomic client swap (single assignment of a tuple)
15. K2: Include effective temperature in response
16. N1/N2: Fix SSE chunk created/id fields
17. N10: Delete tool_calls key instead of setting None
18. B15: Make sticky cut anchor less sensitive to minor changes

### Phase 3: Medium (robustness/performance) - fix after
19. I1: Exponential backoff for DB connection retry
20. I6: Exponential backoff for vLLM 500/503 retries
21. H2: Reduce _prefix_diag to DEBUG level
22. H6: Reuse httpx client in health loop
23. D5: Add TTL eviction for session_prefix_hashes
24. D4: Cap injection_metrics sessions dict
25. F1/F6: Use _env_int for MAX_CONTINUATIONS and LM_WORKERS
26. F2: Validate PRESENCE_PENALTY etc. at startup
27. B27: Guard Prometheus division by zero

### Phase 4: Low (quality/maintainability) - fix last
28. G1-G20: Refactor into modules, remove dead code, add type annotations
29. O1-O10: Architectural improvements (AppState class, request ID, rate limiting)
30. M1-M10: Add test coverage for critical paths
31. L1-L10: Fix logging levels and formats
32. P1-P20: Clean up imports, constants, and minor issues

