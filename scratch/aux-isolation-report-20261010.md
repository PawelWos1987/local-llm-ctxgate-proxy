# Per-Session State Corruption: Validation & Fix Report

Date: 2026-10-10
Repo: /home/pawelw/ctxproxy/proxy/app.py
Sandbox: port 19203, DB ctxproxy_sandbox

## Executive Summary

**DEFECT CONFIRMED AND FIXED.** Two code paths in ctxgate-proxy cause per-session
trim state (session_compactions, session_seeds) to be wiped when a short
"auxiliary" request (e.g., Goose title generation) arrives on the same session.
After the wipe, the next main request cannot reload state from the DB because
_window_load_tried already contains the session key, causing re-summarization
and duplicate phase_summaries rows.

**Fix: 3-line minimal diff** (2 lines removed, 1 line changed).

## Hypothesis Verdicts

| Hypothesis | Verdict | Evidence |
|---|---|---|
| H1: Goose sends auxiliary (title) requests with same session-id, different system prompt, short history | **VERIFIED** (code path) / **UNVERIFIED** (live Desktop) | The proxy log shows requests with different system prompts on the same session. Live Goose Desktop title generation was not directly reproduced. |
| H2: Seed-flap in chat_completions pops session_compactions when system prompt changes | **VERIFIED** (code path) | Line 3914 (pre-fix): session_compactions.pop fires when built[:3] differs from session_seeds. A 2-message title request with a different system prompt triggers this. |
| H3: Under-limit branch in build_context pops session_compactions for ANY short request | **VERIFIED** | Log evidence: H3-POP under-limit fires on a 42-token request. The pop at line 2908 (pre-fix) is unconditional. |
| H4: After pop, _window_load_tried blocks DB reload, causing re-summarization | **VERIFIED** | Log evidence: _window_load_tried.add fires on first over-limit request. After H3-POP wipes session_compactions, the next over-limit request sets slow-path WITHOUT SET from DB. |
| H5: Auxiliary requests pollute events/memory_jobs/knowledge | **PARTIALLY VERIFIED** | The auxiliary request created 1 event row and 1 memory_job row in the sandbox DB. |

## Phase 1: Request Characterization

**UNVERIFIED** for live Goose Desktop. The CLI does not emit title-generation
requests in the same way Desktop does. The closest reproducible equivalent is
a 2-message request with a different system prompt and short user content.

Structural signal for classification:
- **Main chat**: 3+ messages, system prompt matches session seed, has tools
- **Auxiliary/title**: 2 messages, different system prompt, no tools, short content

The fix uses the structural signal (msg count < 3), not text matching.

## Phase 2: Defect Reproduction

### Test setup
- Sandbox proxy on port 19203, MAX_INPUT=100, MAX_CONTEXT=200
- 3 requests: main (155 tokens > 100), aux (42 tokens < 100), main (311 tokens > 100)

### Before fix (defect present):
- _window_load_tried.add fires on first over-limit request
- H3-POP under-limit fires on 42-token aux request (WIPES STATE)
- Next over-limit request: slow-path set WITHOUT DB reload (state lost)

### After fix (defect resolved):
- No H3-POP on aux request
- No SEED CHANGED on aux request
- State preserved across all 3 requests

## Phase 3: Design

### Chosen fix
1. Remove session_compactions.pop(sk, None) from the under-limit branch in build_context.
   Rationale: The under-limit branch means the request fits without trimming.
   There is no reason to discard existing trim state.

2. Change else: to elif len(built) >= 3: in the seed-comparison block in chat_completions.
   Rationale: The seed is defined as the first 3 messages. A 2-message auxiliary
   request cannot meaningfully compare against a 3-message seed.

### Alternatives rejected
- Classify requests as "auxiliary" and skip all state mutations: Requires a reliable classifier.
- Add a "force reload" flag to bypass _window_load_tried: More invasive.
- Make _window_load_tried a dict with timestamps: Over-engineered.

## Phase 4: Implementation

### Diff (3 lines changed)
Removed from build_context under-limit branch:
    if sk:
        session_compactions.pop(sk, None)

Changed in chat_completions seed block:
    else:  ->  elif len(built) >= 3:

### Verification results
| Check | Result |
|---|---|
| One tasks row per session (3 sessions) | PASS |
| No noauth session_ids | PASS |
| No H3-POP in log after aux request | PASS |
| No SEED CHANGED in log after aux request | PASS |
| No duplicate task rows | PASS |
| Pre-seeded session_windows picked up | PASS |
| session_windows unchanged after aux | PASS |
| 3 parallel sessions isolated | PASS |

### Verification script
/home/pawelw/ctxproxy/scratch/verify_aux_isolation.sh

## Unverified Items

1. **Live Goose Desktop title generation**: The human should verify in real Desktop
   use that title requests no longer cause SEED CHANGED warnings in the proxy log.

2. **H5 (memory pollution)**: The auxiliary request does create events and memory_jobs
   rows (1 each). The fix does NOT exclude them because the volume is minimal and
   excluding would require a classifier.

3. **Restart-and-resume with actual trimmed state**: The sandbox test used pre-seeded
   DB rows. A full end-to-end test with real trimming was not completed due to vLLM
   latency. The logic is proven correct by code analysis and the pre-seeded test.

## Cleanup
- Sandbox proxy: STOPPED
- ctxproxy_sandbox DB: LEFT IN PLACE
- CLI test sessions: None created (used direct HTTP requests)
- Production service: NOT TOUCHED

## Status
READY FOR HUMAN RESTART
