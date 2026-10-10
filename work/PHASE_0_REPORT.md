# PHASE 0 REPORT: Baseline + safety harness

Date: 2026-10-07

## 1. Dev copy / git / hashes
- cp -a /home/pawelw/ctxproxy -> /home/pawelw/ctxproxy-dev (flattened nesting).
- .git carried over; HEAD d412fe9; working tree clean.
- sha256 live==dev for app.py and worker.py (see BASELINE.md). VERIFIED byte-identical.

## 2. Findings re-verification (grep against DEV copies == live)

| ID | Status | Evidence (file:line) |
|----|--------|----------------------|
| F1 root summary tiny | VERIFIED | app.py:276 schema "brief current state"; :794 root via _call_4b json_mode=True system=MISTRAL_SYSTEM_PROMPT; :798-799/:811-812 root_summary=su["current_state"] |
| F2 phase summaries never injected | VERIFIED | phase rows inserted :759-760; only read back :783 into phase_history for the ROOT step; fetch_task_memory (:2724) never selects phase_summaries |
| F3 root input lossy [-6000:] | VERIFIED | app.py:787-788 "if len(phase_history)>6000: phase_history=phase_history[-6000:]" |
| F4 retry dup phases + watermark block | VERIFIED | :726 MAX(phase_number); :759-760 insert ON CONFLICT(task_id,phase_number) DO UPDATE; :842-843 watermark advanced ONLY if quality_ok |
| F5 only user msgs to worker | VERIFIED | call site :3221 _enqueue_memory_job(x_sid,last_user_content); def :4521; :4535 len<30 return; :4549-4553 head3000+tail1500 |
| F6 injection lossy/unstable | VERIFIED | :897 s[:budget*4]; :2696 _already_in_context; :2705 present>=0.6*len(uniq); :2788/:2795/:2812 suppression; ILIKE term match in fetch_task_memory |
| F7 injection busts prefix | VERIFIED | :3300-3316 block appended to built[last_user_idx] content; fingerprint :3320 _prefix_raw(built) |
| F8 dangling tool call in seed | VERIFIED | :1960 missing_tool_result check; seed=built[:3] at :3248/:3263; 258 log hits |
| F9 worker defects | VERIFIED | claim_jobs :529-545 SELECT..FOR UPDATE SKIP LOCKED outside txn then separate UPDATE; _norm :162-165 [^a-z0-9]+; CONSUMERS :47 default 10; TEMP :50 0.7; _atomic_write :814-823 log.warning 1 placeholder 2 args; "4B/LM Studio/Qwen3" :1,4,10,13,49,74,93,219,350,489 |
| F10 app vs worker stuck conflict | VERIFIED | app.py:584 processing>120s -> failed; worker recover_stuck_jobs(300) :547,:784 |
| F11 dead/buggy code | VERIFIED | _store_memory_actions :475, _update_working_memory :545; SUPERSEDE :523-529 "with pool.acquire()" (non-async) + "async with conn.transaction()". NOTE: referenced by tests/test_j3_supersede.py:98,115 and _local/scripts/patch_fixes.py (not live request path) |
| F12 summarizer input fidelity | VERIFIED | per_cap 500 :686 / 300 :718 / 200 :721; head60/tail40 :701; SUMMARY_TOOL_CAP_CHARS=1000 :916; args>300 cap :664 |

All 12 findings VERIFIED against live code. Line numbers match the plan's analysed copies closely.

## 3. Baseline metrics
See BASELINE.md. Key: stable ratio mean 0.8503 (n=411); vLLM prefix hit 74.4%; build_context p50 67ms; 32 re-cuts; 258 missing_tool_result.

## 4. Harness + reference
- tests/harness_window.py: NOT YET BUILT (next action).
- tests/reference_prefix.json: NOT YET GENERATED.

## Gate G-PREFIX
Reference not yet generated. Will be the acceptance baseline for all later phases.

## Risks
- F11: _store_memory_actions/_update_working_memory ARE referenced by tests/test_j3_supersede.py and _local/scripts/patch_fixes.py. Deleting them breaks those tests. Decision: neutralize (keep functions, fix async-with) rather than delete, OR delete + update the test. Will decide in Phase 1 (rule 4.5: additive/reversible preferred -> fix async-with, keep functions, mark deprecated).
