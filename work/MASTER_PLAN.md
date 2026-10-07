# MASTER PLAN: session-memory overhaul for ctxgate-proxy (app.py + worker.py)

You are an autonomous senior engineer. This file is your complete brief. You will lose conversational memory
(Goose compaction is off and the proxy window trims your history), so **this file and the state files below are your memory.**

## 0. FIRST ACTIONS and RESUME PROTOCOL

1. If this text arrived as a chat message, save it verbatim to /home/pawelw/ctxproxy-dev/work/MASTER_PLAN.md (create dirs).
2. **At the start of every turn and after anything that looks like lost context, do this before anything else:**
   cat /home/pawelw/ctxproxy-dev/work/MASTER_STATE.md then re-read the section of this plan for the current phase.
3. Never ask the user whether to continue. Make the decision yourself using the rules in section 4, log it in DECISIONS.md, continue.
   Stop only when section 4.6 says so.

State files (all in /home/pawelw/ctxproxy-dev/work/, markdown, keep each short and current):

| File | Purpose |
|---|---|
| MASTER_STATE.md | <=120 lines. Current phase/task, status table, NEXT ACTION: line, last green commit. Update BEFORE and AFTER every task. |
| DECISIONS.md | Append-only log: date, decision, alternatives, why. |
| BASELINE.md | Phase 0 measurements (prefix stability, latencies, file hashes). |
| PHASE_<n>_REPORT.md | Per phase: what changed (files/functions), tests run + results, gate results, risks. |
| OPEN_RISKS.md | Anything reverted, skipped, or unverified. |
| TOOL_SHAPES.md | Real Goose tool-call shapes discovered in Phase 3. |
| DEPLOY.md, FINAL_REPORT.md | Written at the end. |

## 1. Mission and success criteria

Problem: after a ~15M-token session the model only remembers the 3 frozen seed messages and the last task. It forgot that it
produced a DFMEA, a PFMEA and an implementation plan. Root causes are verified in section 3.

Success means ALL of these:

- **S1 Recall.** In a replay of the real session (Phase 6), the ledger/digest injected for a recap question contains every deliverable
  file that exists on disk and was written by a successful tool call, plus test/build results. Probe list (the model itself listed these,
  so verify each on disk and treat the list as a probe, not ground truth): /home/pawelw/goose-scratch/ files
  cv-fmea-20261007.md, cv-implementation-plan-20261007.md, pii-residual-scan-20261007.md, NOTES.md,
  cv-architecture-master-20261006.md, cvparser-flow-20261006.md, matcher-flow-20261006.md, bff-frontend-flow-20261006.md;
  test counts 570/173/107/359.
- **S2 No regression of the rolling window.** Prefix stability and all section 2 invariants hold (measured, not assumed).
- **S3 Worker is correct under concurrency** (no double-claim, per-task ordering, no data loss on QC, Unicode-safe keys).
- **S4 Everything is idempotent and restart-safe**, and all new state is in Postgres or markdown, never only in process memory.

## 2. HARD INVARIANTS (do NOT break; they are why the proxy works today)

The window works well in a 15M-token session with prefix-cache hit rate above 72%. Therefore:

- Do not change the values or logic of: MAX_CONTEXT, MAX_INPUT, TRIM_TARGET_*, SAFETY_MARGIN, MIN_OUTPUT, MAX_OUTPUT,
  _output_budget, _emergency_shrink, _stable_elide_inplace / _elided_tool_content, the sticky-cut fast/slow path,
  _new_window_state, _window_valid, _window_persist/_load, _make_pinned_copy / pinned-copy placement (kept.insert(4, ...)),
  the seed freeze of the first 3 messages, SSE heartbeat, continuation logic, or vLLM sampling logic.
- STUB_TEXT stays byte-identical.
- The system prompt is never mutated.
- **Inside a window epoch, the bytes of every message sent to vLLM, except newly appended messages, must be identical between consecutive
  requests.** An epoch ends only at (a) a re-cut (Context over limit ... re-cut to low-watermark), (b) a new real user message,
  (c) a proxy restart. Prefix breaks at exactly those three points are expected and already happen today (a new user message toggles
  the pinned copy at index 4, which explains stable_msgs=3 of 47 in the 10:48:26 log; confirm by diffing, do not assume).
- Nothing new may be awaited on the request critical path unless it already was (no new LLM call, no new slow DB call per request).
  Fast-path build_context is 61-80 ms at msgs~210 today (the 50 ms SLOW threshold already warns); it must not get worse by more than 10%.
- DB changes are additive only (CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, new indexes). Never drop/alter existing
  columns or tables. Keep all existing columns populated as before so dashboards (/api/memory*) keep working.
- Every new behavior sits behind an env flag. Defaults are conservative: ON only after its gate passes, otherwise OFF.

## 3. VERIFIED FINDINGS (re-verify each with grep against the LIVE files; line numbers are from the analysed copies, app.py 5759 lines, worker.py 965 lines)

**F1. The injected summary is tiny by construction.** The root summary (in _summarize_trimmed_messages) is produced with
MISTRAL_SYSTEM_PROMPT + json_mode=True and read from state_update.current_state, which the schema describes as "brief current state".
Observed output: 68-169 chars. This is the only summary injected into prompts.

**F2. Phase summaries are never injected.** They contain the useful labeled fields (COMPLETED with evidence, DO NOT REDO, ...),
600-2000 chars each, and are used only as input to the root step.

**F3. Root input is lossy.** Root input = prior root + phase_history[-6000:]. This keeps the newest chars and drops the oldest phases,
and 11 phases x 1-2k chars already exceed 6000. Summary-of-summary drift follows.

**F4. Retry duplicates phases and blocks the watermark.** If the root quality gate fails (for example "identical to prior summary", which F1
makes likely), summarized_through is NOT advanced, so the same slice is re-derived and re-summarized, and each retry inserts NEW phase rows
(phase_number = MAX+1+ci) for the same messages.

**F5. Only user messages reach the worker.** _enqueue_memory_job is called with the last user message only (call site ~3221, def ~4521),
inserts role='user', skips text <30 chars, and truncates to head 3000 + tail 1500. Log evidence: only 6 jobs (seq 7..12) in ~12 hours.
The worker's assistant/tool branches are dead code. The DFMEA/PFMEA/plan work happened in assistant/tool turns, so it was never extracted.

**F6. Injection is lossy and unstable.** fetch_task_memory (~2724): the summary is cut with s[:budget*4] (tail lost), and the whole summary
is dropped if _already_in_context finds >=60% of its words in the current window (the window contains the seed task, so a short summary
that echoes it can be suppressed). Relevant memories are chosen by ILIKE on terms of the current messages, so "what did we do?" matches nothing.

**F7. Injection can bust the prefix cache.** The memory block is appended to the content of the LAST user message in built (~3300-3316).
After a cut, that message can be the pinned copy at index 4. Today the text is mostly stable, but as soon as WM/memories/summary change
(more worker output will make this frequent), every change rewrites index 4 and invalidates ~50k tokens of prefix. Per-request term-matched
memories are a second source of variation.

**F8. Dangling tool call in the seed.** Log: missing_tool_result: assistant at ctx[2] declares tool_call chatcmpl-tool-96c2... on EVERY request,
fast and slow path. The frozen seed is built[:3] = system, user, assistant, and seed assistant message [2] carries a tool_call whose result
(original index 3) is outside the seed and cut. The invariant check (5) only warns, so vLLM receives an assistant tool_call with no tool
result every turn.

**F9. Worker defects (worker.py).**
- claim_jobs: SELECT ... FOR UPDATE SKIP LOCKED runs outside a transaction, so the row locks vanish immediately and N consumers can claim
  the same job.
- No per-task ordering: 10 consumers can process events of one task concurrently, so SUPERSEDE/WM can be applied out of order (last writer wins).
- _norm uses [^a-z0-9]+, which erases Polish diacritics. Titles collide, and an all-non-ASCII title gives an empty key_norm.
- NEW with an existing (task, category, key_norm) overwrites the value in place and loses the earlier fact.
- QC self-review sees only its own output (not the source), so it cannot judge hallucination, and a double failure discards ALL entries.
- task_desc is just "session=<id>" (the 4B model never sees the task). WM is overwritten on every job even when changed=false.
- The model must emit source_event_id, which the worker ignores (it uses the real event id). Temperature 0.7 is high for extraction.
- _atomic_write: log.warning("... %s", path, e) has 1 placeholder and 2 args (logging error).
- 10 consumers, no rate limiter, shares the Mistral key with app.py's summarizer. Failed jobs are never retried or surfaced.
- Docstrings/logs still say "4B", "LM Studio", "Qwen3-4B" while Mistral is used.

**F10. app.py vs worker conflict.** _memory_worker_loop (spawned ~1226) marks any job in processing for >120 s as failed, while the worker
legitimately runs up to 3-4 sequential Mistral calls (300 s timeouts) and backs off on 429. recover_stuck_jobs(300) has the same flaw.

**F11. Dead/buggy code in app.py.** _store_memory_actions and _update_working_memory have no callers (grep the whole repo). Their
SUPERSEDE branch uses with pool.acquire() without async.

**F12. Summarizer input fidelity.** Per-message caps of 500/300/200 chars (head 60% / tail 40%) and tool output <=1000 chars. Phase 11 summarized
12 msgs from only 1982 input tokens. Decisions in long assistant messages are mostly invisible to the summarizer.

Re-verify: for each F-item write VERIFIED / NOT REPRODUCED / CHANGED with the grep/line evidence in PHASE_0_REPORT.md. If the live code
differs, adapt the fix to the live code, not to this text.

## 4. OPERATING RULES

4.1 **Dev copy only.** cp -a /home/pawelw/ctxproxy /home/pawelw/ctxproxy-dev (and the worker dir; locate it, expected <repo>/worker/worker.py).
git init there if not a repo; commit the untouched baseline as baseline. Record sha256 of the live proxy/app.py and worker/worker.py in
BASELINE.md. Other agents (C++ port, BUG_CATALOG fixes) may be editing the live tree; never edit the live files and never restart the live
proxy (127.0.0.1:9201) or the live worker. They serve your own session.

4.2 **Test isolation.** Create ctxproxy_test (pg_dump --schema-only of the live DB, restore into the new DB). If you cannot create a DB,
use a separate schema. Never run tests against the live DB. DSN env: CTXGATE_DB_DSN / CTXPROXY_DB_DSN. Run the dev proxy on port 9202 only if needed.
No vLLM generation calls in tests. Mock Mistral in unit tests; at most 20 real Mistral calls total for prompt canaries.

4.3 **Read-only data sources.** vLLM http://127.0.0.1:29000/metrics (read-only, for baseline prefix-cache hit counters), proxy.log, and a
COPY of the Goose sessions DB (GOOSE_SESSIONS_DB, default ~/.local/share/goose/sessions/sessions.db; copy via the sqlite backup API, never
open the live file for writing).

4.4 **Per-phase loop.** Implement, python -m py_compile, unit tests, replay/gate checks, write PHASE_<n>_REPORT.md, git commit
(tag phase-<n>-ok), update MASTER_STATE.md, start the next phase immediately in the same turn.

4.5 **Decision rule.** When ambiguous, pick the option that (1) preserves section 2 invariants, (2) is deterministic over LLM-based,
(3) is additive and reversible, (4) is simplest. Log it. Do not ask.

4.6 **Stop conditions.** (a) A phase gate fails after 3 genuinely different fix attempts: git revert the phase to the last green tag,
record it in OPEN_RISKS.md, continue with the next phase that does not depend on it. (b) Anything needing a live restart or destructive
operation: put exact steps in DEPLOY.md instead of doing it. (c) All phases are done or blocked: write FINAL_REPORT.md, end the turn
with a short summary. This is the only place you may end the turn.

## 5. PHASES

### Phase 0: Baseline and safety harness (no behavior change)
1. Dev copy, git baseline, hashes (4.1). Re-verify F1-F12.
2. Baseline metrics into BASELINE.md: from proxy.log, the per-request PREFIX stable_msgs ratios and re-cut spacing (last 24 h);
   from vLLM /metrics, prefix cache hits/queries counters; p50/p95 of build_context fast and slow path (log SLOW lines).
3. Build tests/harness_window.py: imports the dev app module without starting servers (pool=None must not crash it), replays a
   message history through build_context request by request (history grows one or two messages per request, exactly like Goose resending the
   full history), and records per request: kept token count, SHA of every sent message, and the index of the first differing message vs the
   previous request. Feed it first with synthetic histories (tool loops, new user messages) then with the real session from the Goose DB copy.
4. Run it against the UNMODIFIED code and save the result as the reference (tests/reference_prefix.json).
   **Acceptance gate G-PREFIX (used after every phase):** for the same input, the first-differing-index sequence equals the reference
   except where a phase explicitly intends a change (document it); mean stable-prefix ratio drops by <=1 percentage point; no new
   mid-epoch prefix break.

### Phase 1: Zero-risk correctness fixes
**worker.py**
- W1 Atomic claim. One statement UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) ... RETURNING id, task_id, event_id, attempts.
  Per-task ordering: at most one job in flight per task, enforced race-free. The worker is single-instance (flock), so use an in-process
  inflight_tasks set guarded by an asyncio.Lock around the claim, excluding those tasks in the candidate query (DISTINCT ON (task_id),
  earliest first), plus the DB processing state for crash recovery. Remove the redundant re-UPDATE in process_job.
- W2 _norm: Unicode-aware (NFKC + casefold, keep letters/digits via \w with re.UNICODE); if the result is empty use a short hash of the raw title.
- W3 NEW collision: near-duplicate content (token overlap) -> touch timestamp only. Materially different content -> SUPERSEDE the old row
  (old row stays, active=false, status='superseded', superseded_by). Never overwrite a different value in place. UPDATE keeps its semantics.
- W4 Replace the LLM self-QC with deterministic grounding: every path-like or identifier-like token in a memory's title/content must occur in
  the source payload, else drop THAT entry only. On total failure keep nothing but never discard entries that passed. Saves 1-3 Mistral calls per job.
  (If you keep any LLM QC, it must receive the source excerpt, and double failure must keep CRITICAL/HIGH entries that pass grounding.)
- W5 task_desc: the first user event of the task (seq lowest, <=1500 chars) plus the task name; WM as now.
- W6 WM: write only when su.changed is true and the state is non-empty; never overwrite with empty/null.
- W7 Remove source_event_id from the model schema/validator (set from the real event). TEMP=0.1 for extraction (env-overridable).
- W8 Stuck-job handling: refresh claimed_at of in-flight jobs every 30 s (heartbeat task); recover_stuck_jobs stale threshold >= 600 s;
  per-call timeout 120 s (match MISTRAL_TIMEOUT).
- W9 Rate control: CONSUMERS default 4; token-bucket CTXGATE_WORKER_RPM; on 429 honor Retry-After if present.
- W10 Failed jobs: hourly retry of failed rows with attempts < hard cap (env), count of failed/dead in the status file.
- W11 Fix _atomic_write logging; rename "4B/LM Studio" wording to "memory LLM (Mistral)"; warn (do not crash) if DSN contains CHANGE_ME.
**app.py**
- A1 Remove (or neutralize to log-only) the stuck-job failing logic in _memory_worker_loop; the worker owns recovery (F10).
- A2 Delete dead _store_memory_actions / _update_working_memory only after grepping the whole repo and dashboards for any use (F11); otherwise
  fix async with.
**Tests (real Postgres test DB):** 50 concurrent claims -> each job claimed exactly once; two jobs of one task never overlap; Polish
titles ("Za\u017c\u00f3\u0142\u0107 g\u0119\u015bl\u0105 ja\u017a\u0144") get distinct non-empty keys; SUPERSEDE keeps history; QC grounding drops only ungrounded entries.
Gate: G-PREFIX unchanged (Phase 1 does not touch the request path).

### Phase 2: Seed dangling tool-call repair (request path, tiny, deterministic)
Goal: no missing_tool_result violation on any request, without changing seed freeze or the stored seed.
- Implement a pure function _repair_dangling_tool_calls(msgs) -> list applied to the final built just before the fingerprint and vLLM body
  (so the fingerprint equals what is sent). For each assistant message whose tool_calls have no matching later tool message in msgs, return a
  COPY with those calls removed (drop the key if none remain) and, if content is empty, a fixed short deterministic placeholder
  (for example "[earlier tool call archived]"). Never mutate session_seeds, messages, or built in place elsewhere.
- Requirements: idempotent; byte-identical output for the same input on every request (so the prefix cache stays warm after the single
  unavoidable one-time miss on the first request after deploy); applies on fast, slow and emergency paths; the final tool-call turn of a live
  agent loop (call + result present) is never altered.
- Verify the live Qwen chat template accepts the repaired message (a placeholder content when content was empty).
- Env flag CTXGATE_REPAIR_DANGLING_TOOLCALLS (default 1 after gate).
Gate: invariant (5) reports zero violations over the full replay; G-PREFIX shows the intended single change at index 2 only, then stable.

### Phase 3: Durable deterministic ledger + summarizer fixes (background only)
1. Migration (additive, run at startup in the same place existing DDL is run): proxy.session_ledger(id bigserial pk, task_id uuid, kind text,
   title text, detail text, evidence text, status text, source text, slice_start int, slice_end int, phase_number int, dedupe_hash text,
   created_at timestamptz default now()) with UNIQUE(task_id, dedupe_hash). Kinds: ARTIFACT, TEST_RESULT, MILESTONE, DECISION, FAILURE, TODO,
   CONSTRAINT. Ledger rows are NEVER pruned. Order = id (slices are processed strictly in order under the per-task lock).
   proxy.phase_summaries: add slice_start int, slice_end int, chunk_idx int (nullable).
2. Deterministic extractor (pure function, no LLM, runs on the FULL-FIDELITY dropped slice, not on the capped chunk text):
   - Discover real shapes first: sample actual tool_calls from the Goose DB copy and the proxy logs; record the mapping
     tool_name -> (op, path_arg) in TOOL_SHAPES.md. Do not assume tool names. Known candidates to confirm: a text-editor tool with
     write/str_replace commands, a shell tool whose command writes files (>, tee, cp, mv, heredoc).
   - Pair each call with its tool result via tool_call_id. Success -> ARTIFACT (created/modified, path); error markers -> FAILURE.
   - Test/build lines in tool results: for example Tests run: N, Failures: F, N passed, BUILD SUCCESS|FAILURE, to be validated on real data.
   - Insert with ON CONFLICT (task_id, dedupe_hash) DO NOTHING (dedupe_hash = sha1(kind|normalized path/command|op)); re-running a slice is a no-op.
3. Summarizer (_summarize_trimmed_messages) changes:
   - Run the extractor, persist ledger rows, THEN call the LLM. Ledger success never depends on Mistral.
   - Idempotent phases: key by (task_id, slice_start, slice_end, chunk_idx); a retry upserts instead of creating new phase numbers.
   - **Watermark independence (fixes F4):** advance summarized_through once all chunk phases and ledger rows for the slice are stored. The root/digest
     step must never gate the watermark.
   - Better phase prompt: keep the six labeled fields, add ARTIFACTS: (every file created/modified, one line each with purpose) and
     VERIFICATION: (tests/builds seen). Raise assistant-text fidelity (assistant text carries decisions) and keep tool output capped; keep total
     tokens per trim cycle within about 1.5x today's cost. Plain text only.
4. Replace the root step. New _build_session_digest(task_uuid), composed deterministically from: (a) ledger rows in id order, (b) the newest phase's
   CURRENT TASK / IN PROGRESS / NEXT STEP / DO NOT REDO fields, (c) earlier phases' COMPLETED/DECISIONS lines. An LLM rollup of old phases is optional;
   if used it must use a plain-text prompt (never the JSON memory schema) that says "preserve every item, shorten wording only", and its output
   must pass a validator: every ledger artifact path of the covered range appears in it, else fall back to the deterministic text.
   Store the digest in proxy.session_summaries (same table/columns, summary <= existing cap) so _fetch_session_summary and the dashboards keep working.
Tests: slice re-run creates no duplicate phases or ledger rows; failing Mistral still advances the watermark; extractor recall on the real session
(S1 probe list). Gate: G-PREFIX unchanged (all of this is background).

### Phase 4: Cache-safe injection + recap path (request path, the delicate one)
1. **Epoch freeze (fixes F7).** Compute the whole injected block (knowledge + task memory + digest) ONCE per epoch key
   (session_key, ws.cut, newest-user anchor, recap_flag), store the exact string in the in-memory window state, and reuse it byte-for-byte until the key changes.
   Do NOT recompute WM/memories/digest per request. A fresh DB read happens only when the epoch key changes. Keep the existing append-to-last-user-message
   placement and the existing "TASK STATE (background only ...)" wording.
2. **Re-cut synchronicity.** Ledger rows for a newly dropped slice come from the pure extractor, which is fast, so at re-cut run the extractor on the
   newly dropped slice immediately (CPU only; persist in the background) so the refreshed block already contains it. The LLM phase text may lag one epoch. That is fine.
3. **Digest budgeting.** New _fit_to_budget(sections, budget_tokens): drop the OLDEST ledger lines first, never cut mid-line, never lose the newest
   phase's NEXT STEP / DO NOT REDO. This replaces the s[:budget*4] tail cut. Default CTXGATE_INJECT_MAX_TOKENS 3000; recap turns up to 5000;
   hard-clamp to ceiling - MAX_INPUT - 500 (ceiling = min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT) as computed in the live code; verify how
   kept_tokens and the re-cut trigger are computed relative to injection so the output budget never falls below MIN_OUTPUT and _emergency_shrink is never triggered by injection).
4. **Recap intent** (deterministic, English + Polish keyword/regex, evaluated only when a new user message creates a new epoch):
   examples "what did we do", "summari[sz]e (the )?session", "recap", "status", "co zrobili[\u015bs]my", "podsumuj", "co jest zrobione". On match, include the
   full ledger plus typed DECISION/FAILURE memories chronologically instead of ILIKE term matching.
5. **Suppression fix.** _already_in_context must not suppress the digest or the ledger; keep it only for individual relevant-memory lines.
   Keep working-memory suppression if it is harmless; document the choice.
6. Env flag CTXGATE_INJECT_EPOCH_FREEZE (default 1 after gate) with an instant fallback to the old path.
Gate (hard): G-PREFIX within an epoch is byte-stable even when WM/memories/ledger change in the DB between requests (test by mutating the DB
mid-replay); the new block appears at the next epoch only; replay shows prefix ratio drop <=1 pp vs reference; injection never pushes max_tokens < MIN_OUTPUT;
fast-path latency not worse than +10%.

### Phase 5: Worker sees the work that left the window
- When _summarize_trimmed_messages builds its chunks, ALSO enqueue one proxy.events row per chunk (role='context_slice', content = the chunk text, additive
  meta jsonb column with {slice_start, slice_end, chunk_idx}) plus a memory_jobs row, idempotent by that key (retrying a slice enqueues nothing new).
  Keep the existing user-message enqueue unchanged except to drop the <30 chars rule only if you can show it loses real instructions.
- worker build_payload: handle context_slice with its own label; allow up to CTXGATE_WORKER_SLICE_CHARS (default 12000) for that role. Types
  stay DECISION/FINDING/FAILURE/TODO/CONSTRAINT/FILE/STATE/FACT (add MILESTONE only if the category column accepts it; check for a CHECK constraint
  first). Worker output feeds the memories used by recap and relevant-memory retrieval. It must never affect window bytes except through the epoch-frozen block.
- Decision rule on cost: if the soak test (Phase 6) shows 429s or queue lag above 10 minutes, default this feature OFF and document it.
Tests: exactly-once enqueue per slice across retries and restarts; per-task ordering; no loop (worker output never creates jobs).

### Phase 6: End-to-end validation (replay the real session)
1. Replay the real session history from the Goose DB copy through the dev build_context with real slice summarization (mocked or
   canary Mistral), the extractor, the worker (test DB), and recap questions at several points.
2. Report in ACCEPTANCE.md: S1 recall vs the probe list (each probe: found in ledger Y/N, exists on disk Y/N); G-PREFIX numbers vs reference;
   invariant violations (must be 0); max injection tokens; build_context p50/p95; worker queue lag; duplicate counts (must be 0).
3. Fix what fails (back to the relevant phase), otherwise continue.
4. Optional (only if everything above is green): profile the fast path (61-80 ms) and the 53 ms token count; any optimization must produce
   byte-identical output (prove it with the harness). Skip if it needs risky changes.

### Phase 7: Release candidate
- Write DEPLOY.md: exact commands to apply the patch to the live tree (check live sha256 vs BASELINE.md; if changed, 3-way merge from baseline),
  additive migrations, env flags and defaults, restart order (worker first, then proxy), post-deploy checks (/health, PREFIX log lines,
  zero missing_tool_result, ledger rows appearing), and one-command rollback (git revert/previous files; migrations are additive so no DB rollback needed).
  Note the one-time prefix miss on the first request after restart.
- Write FINAL_REPORT.md: what changed per file/function, evidence per success criterion, remaining risks. Do NOT restart live services.

## 6. Coding rules
- English comments; small, reviewable diffs; no refactors outside this scope; no new dependencies unless unavoidable (log it).
- Every new DB write is idempotent. Every new async background task has a lock or in-flight guard and cleans up in finally.
- Every claim in a report cites a command output or a file path/line. Mark unverified items UNVERIFIED in OPEN_RISKS.md.
