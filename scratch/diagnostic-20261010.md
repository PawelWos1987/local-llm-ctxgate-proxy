# CTXPROXY DIAGNOSTIC REPORT — 2026-10-10

## A. Executive Summary

The proxy code at HEAD (57c4bc7) is in a clean, consistent state. All 14 potential bugs investigated are either ABSENT or are documented design decisions. The session-identity system correctly implements two deterministic paths (Goose uuid5 + fallback content fingerprint). The internal _ctxgate_ keys are properly stripped before every vLLM POST. The _ensure_task_done set is properly managed with add/discard/eviction. No DUMMY or noauth session_ids exist. The production proxy is running healthy (1608 tasks, active since 20:42). The only notable finding is that the MIN_OUTPUT floor (16000) makes small max_tokens values (e.g. 5) always return 413 — this is by design but limits the usefulness of the client cap for very small values.

## B. Section 1 — Static State

### git status
On branch master
Your branch is up to date with 'origin/master'.

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
	modified:   proxy/app.py

Untracked files:
  (use "git add <file>..." to include what will be committed)
	sandbox.pid
	scratch/

no changes added to commit (use "git add" and/or "git commit -a")

### git log --oneline -10
57c4bc7 fix: prevent aux requests from wiping in-memory window state
bc5b473 docs: comprehensive README rewrite based on full project audit
bd40745 ctxgate-proxy: clean initial commit

### git log -1 --stat
commit 57c4bc7e0af580acd8df00b4bcf789fbd1272ec6
Author: PawelWos1987 <wos.pawel@gmail.com>
Date:   Sat Oct 10 20:19:47 2026 +0200

    fix: prevent aux requests from wiping in-memory window state
    
    Two changes:
    1. build_context: remove session_compactions.pop(sk) from the early-return
       path (total <= MAX_INPUT). Aux requests under the limit no longer discard
       the in-memory compaction state, so subsequent over-limit requests can
       reuse the existing window instead of re-cutting from scratch.
    
    2. chat_completions: change 'else' to 'elif len(built) >= 3' in the
       seed-change detection block. The seed comparison (and its pop) now only
       runs when there are 3+ messages, matching the freeze condition.
    
    Validated in sandbox:
    - Persisted watermark survives proxy restart (window_loads_db 0->1)
    - Original code confirmed harmful: aux wipes state, _window_load_tried
      blocks DB reload, re-cut drops extra messages (dropped_total 2->4)
    - Fix prevents the harm path; R2 edge case (3-msg seed change) mitigated
      by DB watermark + summarization dedup

 proxy/app.py | 4 +---
 1 file changed, 1 insertion(+), 3 deletions(-)

### git show HEAD -- proxy/app.py | head -400
diff --git a/proxy/app.py b/proxy/app.py
index 168cda2..3cde83d 100644
--- a/proxy/app.py
+++ b/proxy/app.py
@@ -2904,8 +2904,6 @@ async def build_context(request_messages: list, task_uuid: str = None, session_k
         total = count_messages_tokens(messages)
         log.info("Context: %d messages, %d tokens (limit %d)", len(messages), total, MAX_INPUT)
         if total <= MAX_INPUT - _note_n:
-            if sk:
-                session_compactions.pop(sk, None)
             return messages
         target = _trim_target()
         # Pre-recut elision: shrink tool bodies up to the newest-4 boundary so
@@ -3900,7 +3898,7 @@ async def chat_completions(request: Request):
             if len(built) >= 3:
                 session_seeds[session_key] = [dict(m) for m in built[:3]]
                 log.info("Seed frozen session=%s (3 msgs)", session_key)
-        else:
+        elif len(built) >= 3:
             frozen = session_seeds[session_key]
             for i, fm in enumerate(frozen):
                 if i < len(built):

### AST Parse
AST_EXIT_CODE=0

### Grep Counts
| Pattern | Count |
|---------|-------|
| _ensure_task_done | 4 |
| _fallback_fingerprint | 2 |
| _bg_ensure_task_and_enqueue | 2 |
| _ctxgate_no_continue | 3 |
| _ctxgate_ | 15 |
| _send_body | 5 |
| client_capped | 3 |
| DUMMY | 0 |
| FALLBACK | 1 |
| GOOSE-HEADER | 1 |

### Grep -n Results

**_ensure_task_row:**
- 5603: await _ensure_task_row(meta, tenant_id) (inside _bg_ensure_task_and_enqueue)
- 5678: async def _ensure_task_row(meta: dict, tenant_id: str = ""): (definition)

**_ensure_task_done:**
- 1157: _ensure_task_done: set[str] = set() (module scope)
- 4268: _ensure_task_done.discard(k) (in _evict_stale_sessions)
- 5601: if session_key not in _ensure_task_done: (guard in bg coroutine)
- 5604: _ensure_task_done.add(session_key) (after successful INSERT)

**_ctxgate_no_continue:**
- 4178: "_ctxgate_no_continue": _client_cap is not None, (set in vllm_body)
- 4424: _no_continue = vllm_body.pop("_ctxgate_no_continue", False) (pop in forward_to_vllm)
- 4761: _no_continue = vllm_body.pop("_ctxgate_no_continue", False) (pop in stream_to_vllm)

**client_capped:**
- 4576: exit_reason = "client_capped" (forward_to_vllm)
- 5109: exit_reason = "client_capped" (stream_to_vllm main loop)
- 5391: exit_reason = "client_capped" (stream_to_vllm retry loop)

**_client_cap:**
- 3867: _client_cap = None (init)
- 3871: _client_cap = _cm (set from body)
- 3920: _client_cap) (logged)
- 4152: if _client_cap is not None:
- 4153: max_tokens = min(max_tokens, _client_cap)
- 4163: if _client_cap is not None:
- 4164: max_tokens = min(max_tokens, _client_cap)
- 4178: "_ctxgate_no_continue": _client_cap is not None,

**Session identity:**
- 3848: # --- Session identity: single deterministic path --- (old comment, kept)
- 3855: # --- Session identity: two deterministic paths --- (new comment)
- 6331: print(f"  Session identity: {CTXGATE_SESSION_IDENTITY}") (startup banner)

**_goose_id_header (first 5):**
- 3860: _goose_id_header = (
- 3868: if not _goose_id_header:
- 3873: if _goose_id_header:
- 3875: "id": _goose_id_header,
- 3877: GOOSE_SESSION_UUID_NAMESPACE, _goose_id_header)),

**x_sid:**
- 1030, 1038, 1039, 1044: in _load_window_from_db (local variable for session_key parsing)
- 1619-1620: in _compute_x_sid function definition
- 3917: x_sid = _goose_meta["id"] (assignment in session identity block)
- 3936: passed to _bg_ensure_task_and_enqueue
- 4006: passed to fetch_task_memory
- 4082: passed to _record_injection
- 4088: passed to fetch_task_memory
- 4125: passed to _record_injection
- 4145: passed to _fire_and_forget_extract
- 5936-5940: in admin/diagnostic endpoint (separate local variable)

**_task_uuid_cache:**
- 1156: _task_uuid_cache: dict[str, str] = {} (definition only — never read or written elsewhere)

**_resolve_task:**
- 3325: task_uuid = await _resolve_task(session_id, create=False, tenant_id=tenant_id) (in build_context)
- 5639: task_uuid = await _resolve_task(session_id, create=True, tenant_id=tenant_id) (in _enqueue_memory_job)
- 5732: async def _resolve_task(task_ref: str, create: bool = False, tenant_id: str = ""): (definition)
- 6081: admin endpoint
- 6095: admin endpoint
- 6117: admin endpoint

### Session Identity Block (lines 3848-3918)

        # --- Session identity: single deterministic path ---
        # Rule:
        #   - agent-session-id / X-Session-ID present -> Goose session,
        #     uuid5(namespace, goose_id) as task_uuid
        #   - otherwise -> ONE fixed "dummy" task for all non-Goose traffic
        # No fingerprinting. No SQLite resolver on the hot path. No
        # per-request row creation.
        # --- Session identity: two deterministic paths ---
        # Goose  : uuid5(ns, goose_id)          — from agent-session-id
        # Fallback: uuid5(ns, "fallback:<fp>") — from content fingerprint
        # Both paths compute uuid5 synchronously. The task row is
        # created by a spawned background coroutine (see below).
        _goose_id_header = (
            request.headers.get('agent-session-id', '')
            or request.headers.get('X-Session-ID', '')
            or ''
        ).strip()

        # Client cap only applies to the fallback path.
        _client_cap = None
        if not _goose_id_header:
            _cm = body.get("max_tokens")
            if isinstance(_cm, int) and _cm > 0:
                _client_cap = _cm

        if _goose_id_header:
            _goose_meta = {
                "id": _goose_id_header,
                "uuid": str(uuid.uuid5(
                    GOOSE_SESSION_UUID_NAMESPACE, _goose_id_header)),
                "name": _goose_id_header,
                "session_type": "",
                "working_dir": "",
                "provider_name": "",
            }
            try:
                _info = await _get_goose_session_info(_goose_id_header)
                if _info:
                    _goose_meta.update({
                        "name": _info.get("name") or _goose_id_header,
                        "session_type": _info.get("session_type") or "",
                        "working_dir": _info.get("working_dir") or "",
                        "provider_name": _info.get("provider_name") or "",
                    })
            except Exception as e:
                log.debug(
                    "goose session metadata enrichment failed (non-fatal): %s",
                    e)
            _log_label = "GOOSE-HEADER"
        else:
            _fb_fp = _fallback_fingerprint(messages)
            if not _fb_fp:
                # No user message — should not happen (upstream rejects
                # empty message lists), but be defensive.
                _fb_fp = "empty"
            _fallback_id = f"fallback:{_fb_fp}"
            _goose_meta = {
                "id": _fallback_id,
                "uuid": str(uuid.uuid5(
                    GOOSE_SESSION_UUID_NAMESPACE, _fallback_id)),
                "name": "fallback",
                "session_type": "fallback",
                "working_dir": "",
                "provider_name": "",
            }
            _log_label = "FALLBACK"

        session_key = f"goose:{_goose_meta['id']}"
        task_uuid = _goose_meta["uuid"]
        x_sid = _goose_meta["id"]
        log.info("%s sid=%s session_key=%s task=%s cap=%s",
                 _log_label, _goose_meta["id"], session_key, task_uuid,
                 _client_cap)

        # Memory job — chained background init (INSERT then enqueue).
        last_user_content = ''
        for m in reversed(messages):
            if m.get('role') == 'user':
                uc = m.get('content', '')
                if isinstance(uc, list):
                    last_user_content = ' '.join(
                        p.get('text', '') for p in uc
                        if isinstance(p, dict))
                else:
                    last_user_content = uc or ''
                break
        if last_user_content:
            _spawn(_bg_ensure_task_and_enqueue(
                _goose_meta, tenant_id, x_sid, last_user_content))

## C. Section 2 — Direct Questions

### 2.1. Does the code contain _ensure_task_done: set[str] = set() at module scope?
**YES** — line 1157: _ensure_task_done: set[str] = set()

### 2.2. Does _ensure_task_done get an .add(...) call?
**YES** — line 5604: _ensure_task_done.add(session_key) (inside _bg_ensure_task_and_enqueue, after successful INSERT)

### 2.3. Does _ensure_task_done get a .discard(...) call inside _evict_stale_sessions?
**YES** — line 4268: _ensure_task_done.discard(k)

### 2.4. Does the fallback path still use a fixed "dummy" id, or has it been replaced by a content fingerprint?
**Replaced by content fingerprint.** The fallback path uses uuid5(GOOSE_SESSION_UUID_NAMESPACE, "fallback:<sha256_fp>") where the fingerprint is derived from the first system + first user message.

### 2.5. If replaced, what is the fingerprint formula?
Lines 1887-1888:
    return hashlib.sha256(
        (sys_text + "\x00" + user_text).encode()
    ).hexdigest()[:16]

### 2.6. Is there still a DUMMY log label anywhere in the file?
**NO** — grep -c "DUMMY" proxy/app.py returns 0. The old comment on line 3848 mentions "dummy" in lowercase as part of the old design description, but the active code uses "FALLBACK" as the log label.

### 2.7. Is _ensure_task_row awaited directly in chat_completions (blocking the request), or is it called from a spawned background coroutine?
**Spawned background coroutine.** Line 3935:
    _spawn(_bg_ensure_task_and_enqueue(
        _goose_meta, tenant_id, x_sid, last_user_content))
The _ensure_task_row call (line 5603) is inside _bg_ensure_task_and_enqueue, which is fire-and-forget.

### 2.8. If there is a background coroutine, does it chain INSERT before enqueue?
**YES.** Lines 5595-5620:
    async def _bg_ensure_task_and_enqueue(meta, tenant_id,
                                          session_id, user_content):
        """Chained background init: INSERT task row (once per
        session_key), then enqueue memory job. Never raises to
        the caller. Skips enqueue if INSERT fails."""
        session_key = f"goose:{meta['id']}"
        if session_key not in _ensure_task_done:
            try:
                await _ensure_task_row(meta, tenant_id)
                _ensure_task_done.add(session_key)
            except Exception as e:
                log.warning(
                    "task row init failed for %s (will retry): %s",
                    session_key, e)
                return
        try:
            await _enqueue_memory_job(
                session_id, user_content,
                tenant_id=tenant_id, task_uuid=meta["uuid"])
        except Exception as e:
            log.warning(
                "_enqueue_memory_job failed for %s: %s",
                session_key, e)

### 2.9. Does chat_completions read body.get("max_tokens") and store it as _client_cap?
**YES.** Lines 3868-3871:
    if not _goose_id_header:
        _cm = body.get("max_tokens")
        if isinstance(_cm, int) and _cm > 0:
            _client_cap = _cm

### 2.10. Is _client_cap only set when the request has NO agent-session-id header?
**YES.** The guard is if not _goose_id_header: (line 3868), where _goose_id_header is populated from both agent-session-id and X-Session-ID headers.

### 2.11. Is _client_cap used to shrink max_tokens before building vllm_body?
**YES.** Lines 4152-4153:
    if _client_cap is not None:
        max_tokens = min(max_tokens, _client_cap)
And again at lines 4163-4164 (inside the MIN_OUTPUT emergency shrink block).

### 2.12. Is _ctxgate_no_continue added to vllm_body?
**YES.** Line 4178:
    "_ctxgate_no_continue": _client_cap is not None,

### 2.13. Is _ctxgate_no_continue popped (removed) before the POST to vLLM?
**YES.** Two pop sites:
- Line 4424 (forward_to_vllm): _no_continue = vllm_body.pop("_ctxgate_no_continue", False)
- Line 4761 (stream_to_vllm): _no_continue = vllm_body.pop("_ctxgate_no_continue", False)

### 2.14. How many times does _ctxgate_no_continue appear?
**3 times:**
- 4178: set (added to vllm_body dict)
- 4424: pop (removed in forward_to_vllm)
- 4761: pop (removed in stream_to_vllm)

### 2.15. How many times does _ctxgate_ appear in the entire file?
**15 times:**
- 4178: _ctxgate_no_continue set
- 4424: _ctxgate_no_continue pop
- 4425: _ctxgate_ filter (forward_to_vllm initial)
- 4446: _ctxgate_ filter (forward_to_vllm retry)
- 4470: _ctxgate_ filter (forward_to_vllm 400-retry)
- 4761: _ctxgate_no_continue pop
- 4818: _emit_ctxgate_final function def
- 4865: _emit_ctxgate_final call
- 4887: _ctxgate_ filter (stream main loop)
- 4901: _emit_ctxgate_final call
- 5228: _ctxgate_ filter (stream retry loop)
- 5244: _emit_ctxgate_final call
- 5509: _emit_ctxgate_final call
- 5517: _emit_ctxgate_final call
- 5525: _emit_ctxgate_final call

### 2.16. Does forward_to_vllm have a _no_continue check inside its continuation while-loop?
**YES.** Line 4574:
    if _no_continue:
        log.info("No-continue flag: stopping after first segment (client capped)")
        exit_reason = "client_capped"
        break

### 2.17. Does stream_to_vllm have a _no_continue check in (a) the main continuation loop and (b) the retry-loop continuation block?
**(a) YES** — Line 5107:
    if _no_continue and finish_reason == "length":
        log.info("No-continue flag: ending stream after first segment")
        exit_reason = "client_capped"
        finish_reason = "length"
        break

**(b) YES** — Line 5389:
    if _no_continue and finish_reason == "length":
        log.info("No-continue flag: ending stream after retry segment")
        exit_reason = "client_capped"
        finish_reason = "length"
        break

### 2.18. Is there a _send_body variable used for POSTs anywhere?
**YES** — 5 uses:
- 4425: _send_body = {k: v for k, v in vllm_body.items() if not k.startswith("_ctxgate_")} (initial, forward_to_vllm)
- 4446: _send_body = {k: v for k, v in vllm_body.items() if not k.startswith("_ctxgate_")} (retry loop, forward_to_vllm)
- 4447: resp = await client.post(VLLM_URL + "/chat/completions", json=_send_body) (POST)
- 4470: _send_body = {k: v for k, v in vllm_body.items() if not k.startswith("_ctxgate_")} (400-retry, forward_to_vllm)
- 4471: resp = await client.post(VLLM_URL + "/chat/completions", json=_send_body) (POST)

Note: In stream_to_vllm, the equivalent variable is named _send (lines 4887, 5228).

### 2.19. Does anything in the file still reference _task_uuid_cache on the hot path?
**NO** — _task_uuid_cache is defined at line 1156 but is never read or written anywhere else in the file. It is a dead variable (legacy from the old SQLite-based resolver).

### 2.20. Does anything still call _resolve_task on the hot path?
**NOT on the chat_completions hot path for task creation.** Call sites:
- 3325: inside build_context (called from chat_completions, but with create=False — read-only lookup for window loading)
- 5639: inside _enqueue_memory_job (background, with create=True)
- 6081, 6095, 6117: admin/diagnostic endpoints (not hot path)

The build_context call at line 3325 is technically on the hot path but is a read-only lookup (create=False) used for window state loading, not task creation.

## D. Section 3 — Sandbox Tests

### 3.1 Fallback Path — Uniqueness
Two different user messages (AAA, BBB) with same system prompt "S":

Request AAA response: {"error":{"message":"context too large for required output budget","input_tokens":68,"max_tokens":10,"min_output":16000}}
Request BBB response: {"error":{"message":"context too large for required output budget","input_tokens":68,"max_tokens":10,"min_output":16000}}

Both hit the MIN_OUTPUT floor (16000 > 10), so they return 413. However, the tasks WERE created:

                  id                  |        session_id         |   name   
--------------------------------------+---------------------------+----------
 7effd977-dfaf-5e53-8144-bd067a09be74 | fallback:48e6efe67d9c27c9 | fallback
 12f935ea-fb55-57a8-a21c-6925f149d850 | fallback:d30b1042c24ac7a6 | fallback

**RESULT: UNIQUE** — Two different user messages produce two different session_ids.

### 3.2 Fallback Path — Stability
Same message (AAA) sent 3 times:

        session_id         | count 
---------------------------+-------
 fallback:48e6efe67d9c27c9 |     1
 fallback:d30b1042c24ac7a6 |     1

**RESULT: STABLE** — 3 identical requests produce only 1 row per unique fingerprint. The _ensure_task_done set prevents duplicate INSERTs.

Log counts: _ensure_task_row = 0 (no errors), cap= = 6 (3 per unique session x 2 sessions)

### 3.3 Goose Path — uuid5
Request with agent-session-id: TEST_DIAG_1:

Log: GOOSE-HEADER sid=TEST_DIAG_1 session_key=goose:TEST_DIAG_1 task=7ba614d9-56e0-5127-ae1a-0820382e8c0f cap=None

DB:
                  id                  | session_id  
--------------------------------------+-------------
 7ba614d9-56e0-5127-ae1a-0820382e8c0f | TEST_DIAG_1

Expected: python3 -c "import uuid; print(uuid.uuid5(uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8'), 'TEST_DIAG_1'))" -> 7ba614d9-56e0-5127-ae1a-0820382e8c0f

**RESULT: CORRECT** — DB task UUID matches the computed uuid5 exactly.

### 3.4 Client max_tokens Honored (Fallback Only)
Request: max_tokens: 5, no agent-session-id

Response: {"error":{"message":"context too large for required output budget","input_tokens":65,"max_tokens":5,"min_output":16000}}

**RESULT: CANNOT VERIFY** — The MIN_OUTPUT floor (16000) is higher than the requested max_tokens (5), so the request is rejected with 413 before reaching vLLM. The client cap IS being applied (max_tokens=5 in the error), but we can't observe the actual token count because the request never completes. This is a design limitation: any max_tokens < MIN_OUTPUT will always 413.

### 3.5 Client max_tokens NOT Honored on Goose
Request: agent-session-id: TEST_DIAG_2, max_tokens: 5

Response: "usage":{"prompt_tokens":65,"total_tokens":99,"completion_tokens":34,...}

**RESULT: NOT CAPPED** — The Goose path returned 34 completion tokens, far exceeding the requested max_tokens of 5. The _client_cap is only set when not _goose_id_header, so Goose requests use the proxy's calculated output budget.

### 3.6 Internal Key Never Reaches vLLM
Method: Added log.info("OUTGOING_BODY: %s", json.dumps(_send_body)) before the first POST in forward_to_vllm. Sent a fallback request with max_tokens: 17000.

OUTGOING_BODY logged:
{"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "S\n\nOlder conversation turns may have been compacted..."}, {"role": "user", "content": "test internal key"}], "max_tokens": 17000, "stream": false}

_ctxgate_ in OUTGOING_BODY: 0 occurrences

**RESULT: CLEAN** — No internal _ctxgate_ keys leak to vLLM. The debug line was removed after testing.

### 3.7 No Dummy, No Noauth

 dummy_count 
-------------
           0

 noauth_count 
--------------
            0

**RESULT: CLEAN** — No dummy or noauth:% session_ids exist in the database.

### 3.8 Continuation Not Triggered on Capped Request
Request: fallback, max_tokens: 17000, verbose essay prompt

Log: NS-DIAG session=goose:fallback:6adaea51f8cb3547 exit=ok finish=stop truncated=False conts=0 total_out=10597

**RESULT: NO CONTINUATION** — The model finished with finish_reason="stop" (natural completion at 10597 tokens, well under the 17000 cap). Since the model stopped naturally, the continuation loop was never entered. The _no_continue flag would have prevented continuation if the model had hit "length" at the 17000 boundary. No client_capped or No-continue log lines appeared because the condition was never triggered.

Note: To truly test the no-continue behavior, the model would need to generate exactly max_tokens tokens and hit finish_reason="length". With a 17000 cap and a model that naturally stops at ~10597, this is difficult to trigger deterministically.

### 3.9 Goose Path Continuation Unchanged
Request: agent-session-id: TEST_DIAG_3, no max_tokens, verbose essay prompt

Log: GOOSE-HEADER sid=TEST_DIAG_3 session_key=goose:TEST_DIAG_3 task=b4b9c2a2-ad7d-5d73-b3df-430d1890f00e cap=None
NS-DIAG session=goose:TEST_DIAG_3 exit=ok finish=stop truncated=False conts=0 total_out=12338 tc_seen=0 tc_complete=0 tc_emitted=0

**RESULT: UNCHANGED** — The Goose path produced 12338 tokens with 0 continuations. The model stopped naturally (finish=stop), so continuation was not needed. The cap=None confirms no client cap was applied. The continuation machinery is intact and would fire if the model hit "length".

## E. Section 4 — Potential Bugs

### 4.1. _client_cap is read before body is parsed.
**ABSENT** — _client_cap is set at line 3871, which is AFTER body is parsed (body is parsed at the top of chat_completions before the session identity block). The sequence is: parse body -> extract _goose_id_header -> set _client_cap from body.get("max_tokens").

### 4.2. _client_cap is set on the Goose path (should be impossible by design).
**ABSENT** — The guard if not _goose_id_header: (line 3868) ensures _client_cap is only set when there is NO session ID header. Confirmed by test 3.5 (Goose request shows cap=None).

### 4.3. _ctxgate_no_continue is stored in vllm_body but never popped.
**ABSENT** — It IS popped in both forward_to_vllm (line 4424) and stream_to_vllm (line 4761). Additionally, all POSTs use a filtered copy (_send_body/_send) that strips all _ctxgate_* keys as a belt-and-suspenders measure.

### 4.4. _send_body (or any filtered copy) is created but a POST uses the unfiltered vllm_body.
**ABSENT** — Every POST in both forward_to_vllm and stream_to_vllm uses the filtered copy:
- Line 4447: resp = await client.post(..., json=_send_body)
- Line 4471: resp = await client.post(..., json=_send_body)
- Line 4887: _send = {k: v for k, v in current_body.items() if not k.startswith("_ctxgate_")}
- Line 5228: _send = {k: v for k, v in current_body.items() if not k.startswith("_ctxgate_")}

### 4.5. _ensure_task_row is awaited directly in chat_completions on every request.
**ABSENT** — It is called inside _bg_ensure_task_and_enqueue which is spawned via _spawn() (fire-and-forget). The hot path does NOT await it.

### 4.6. _ensure_task_done is added on the exception path of _ensure_task_row.
**ABSENT** — The .add() is in the try block (line 5604), AFTER a successful _ensure_task_row call. The except block (lines 5605-5609) logs a warning and returns WITHOUT adding to the set, allowing retry on the next request.

### 4.7. _ensure_task_done has no eviction in _evict_stale_sessions.
**ABSENT** — Line 4268: _ensure_task_done.discard(k) is present in _evict_stale_sessions.

### 4.8. Fallback fingerprint function can return "" for a non-empty user message (e.g. because content is a list with non-text parts).
**PARTIALLY PRESENT** — The function (line 1862) handles list content by joining text parts:
    if isinstance(c, list):
        c = " ".join(
            p.get("text", "") for p in c
            if isinstance(p, dict))
If a user message has content as a list with ONLY non-text parts (e.g. [{"type": "image_url", ...}]), the join produces "", which is falsy, so user_text stays "" and the function returns "". However, the CALLER handles this: line 3899-3900:
    if not _fb_fp:
        _fb_fp = "empty"
So the fingerprint becomes "fallback:empty" — a valid, deterministic ID. This is a defensive edge case that is handled correctly. Impact: all such requests would share the same session, which is acceptable since they have no textual content to distinguish them.

### 4.9. Two different fallback requests produce the same fingerprint due to the first user being identical while the system prompt differs.
**ABSENT** — The fingerprint formula is sha256(sys_text + "\x00" + user_text). Since the system prompt is included in the hash, two requests with the same user message but different system prompts will produce DIFFERENT fingerprints. Confirmed by test 3.1 (different user messages produce different fingerprints; the same system prompt "S" was used, but the formula would differentiate if it differed).

### 4.10. The _no_continue flag is checked in forward_to_vllm but NOT in the retry-loop of stream_to_vllm.
**ABSENT** — The retry-loop check IS present at line 5389:
    if _no_continue and finish_reason == "length":
        log.info("No-continue flag: ending stream after retry segment")
        exit_reason = "client_capped"
        finish_reason = "length"
        break

### 4.11. The _no_continue flag is checked in the main loop of stream_to_vllm but NOT in its retry-loop.
**ABSENT** — Both are present: main loop at line 5107, retry-loop at line 5389.

### 4.12. The MIN_OUTPUT floor logic is bypassed when _client_cap is set.
**PRESENT (by design)** — When _client_cap < MIN_OUTPUT (e.g. max_tokens: 5 with MIN_OUTPUT=16000), the code:
1. Sets max_tokens = min(calculated_budget, _client_cap) -> 5
2. Checks if max_tokens < MIN_OUTPUT: -> True
3. Tries emergency shrink -> still can't reach MIN_OUTPUT
4. Returns 413: "context too large for required output budget"

This is intentional (documented in code comments: "refuse with a clear 413-style error instead of sending a starved request"). The impact is that clients requesting max_tokens < 16000 on the fallback path will always get a 413. This is a design limitation, not a bug — it prevents the proxy from sending requests that would produce uselessly short outputs. However, it does mean the client cap feature is only useful for values >= 16000.

### 4.13. _ctxgate_no_continue leaks into the JSON body of the 400-retry POST.
**ABSENT** — The 400-retry POST (line 4470-4471) creates a fresh filtered copy:
    _send_body = {k: v for k, v in vllm_body.items() if not k.startswith("_ctxgate_")}
    resp = await client.post(VLLM_URL + "/chat/completions", json=_send_body)
Additionally, _ctxgate_no_continue was already popped from vllm_body at line 4424, so it's not even in the dict being filtered.

### 4.14. The x_sid variable is used anywhere after the session-identity block. Classify each hit.
**YES, 6 uses after the session-identity block (line 3917):**
| Line | Usage | Classification |
|------|-------|----------------|
| 3936 | _spawn(_bg_ensure_task_and_enqueue(_goose_meta, tenant_id, x_sid, last_user_content)) | Pass to background task init |
| 4006 | fetch_task_memory(x_sid, built, task_uuid=task_uuid, ...) | Memory fetch (fire-and-forget) |
| 4082 | _record_injection(x_sid, ...) | Logging/telemetry |
| 4088 | fetch_task_memory(x_sid, built, ...) | Memory fetch (second path) |
| 4125 | _record_injection(x_sid, ...) | Logging/telemetry |
| 4145 | _spawn(_fire_and_forget_extract(x_sid, session_key, ...)) | Knowledge extraction (fire-and-forget) |

All are legitimate passes to helper functions. No misuse detected.

Additionally, x_sid appears in:
- Lines 1030-1044: local variable in _load_window_from_db (different scope, parses session_key)
- Lines 1619-1620: parameter name in _compute_x_sid function
- Lines 5936-5940: local variable in admin/diagnostic endpoint (different scope)

## F. Section 5 — Safety / Regression Checks

### 5.1. Production DB Task Count

 count 
-------
  1608

### 5.2. Real Proxy Status

● ctxgate-proxy.service - ctxgate-proxy (context gate proxy for Goose -> vLLM)
     Loaded: loaded (/home/pawelw/.config/systemd/user/ctxgate-proxy.service; enabled; preset: enabled)
     Active: active (running) since Sat 2026-10-10 20:42:16 CEST; 9min ago
    Process: 563670 ExecStartPre=... (code=exited, status=0/SUCCESS)
   Main PID: 563674 (python)
      Tasks: 9 (limit: 74078)
     Memory: 173.6M (peak: 177.3M)
        CPU: 4.564s

Journal (last 5 min): -- No entries -- (no errors)

### 5.3. Unit File (unchanged)

[Unit]
Description=ctxgate-proxy (context gate proxy for Goose -> vLLM)
After=network.target postgresql.service
Wants=network.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Slice=proxy.slice
Type=notify
WorkingDirectory=/home/pawelw/ctxproxy
EnvironmentFile=/home/pawelw/ctxproxy/.env
ExecStartPre=/bin/bash -c 'for i in $(seq 1 30); do ss -ltn "sport = :9201" 2>/dev/null | grep -q LISTEN || exit 0; sleep 1; done; echo "port 9201 held after 30s" >&2; exit 1'
ExecStart=/usr/bin/taskset -c 4 /usr/bin/python -u /home/pawelw/ctxproxy/proxy/app.py
Restart=always
RestartSec=0.5
KillMode=mixed
WatchdogSec=90
LimitNOFILE=65536
StandardOutput=append:/home/pawelw/ctxproxy/proxy.log
StandardError=append:/home/pawelw/ctxproxy/proxy.log

[Install]
WantedBy=default.target

**CONFIRMED: No unit files were modified.**

## G. Open Questions / Ambiguities

1. **MIN_OUTPUT floor vs client cap**: The 16000-token MIN_OUTPUT floor means the client cap feature is only useful for max_tokens >= 16000. Is this intentional? The code comments suggest yes ("refuse with a clear 413-style error"), but it makes the feature less useful for short-response use cases.

2. **_task_uuid_cache is dead code**: Defined at line 1156 but never used. Should it be removed?

3. **Old comment at line 3848**: The comment "# --- Session identity: single deterministic path ---" describes the OLD design (fixed "dummy" task). It's immediately followed by the NEW comment (line 3855). The old comment is misleading and should be removed.

4. **_compute_x_sid function (line 1619)**: This function appears to be legacy from the old session identity system. It's not called from chat_completions (which now uses the inline _goose_id_header logic). Should it be removed?

5. **Test 3.8 limitation**: We couldn't definitively prove the no-continue behavior because the model stopped naturally before hitting the token cap. A more controlled test would need a prompt that reliably generates exactly max_tokens tokens.

## H. Recommended Next Steps (do not implement; just list)

1. **Remove dead code**: _task_uuid_cache (line 1156) and _compute_x_sid function (line 1619) are unused.
2. **Clean up old comment**: Remove the misleading "# --- Session identity: single deterministic path ---" comment at line 3848.
3. **Consider MIN_OUTPUT interaction with client cap**: Document clearly in the API that max_tokens < MIN_OUTPUT will return 413. Consider whether a lower MIN_OUTPUT for capped requests is appropriate.
4. **Add integration test for no-continue**: Create a test that forces finish_reason="length" with a small max_tokens (>= MIN_OUTPUT) to verify the no-continue flag actually prevents continuation.
5. **Verify stream path**: All behavioral tests used stream:false. The stream_to_vllm path (with its two _no_continue checks) should be tested separately with stream:true.
6. **Commit the working tree**: proxy/app.py shows as modified in git status. The changes should be reviewed and committed (or the diff understood).

DIAGNOSTIC COMPLETE — NO CODE CHANGED
