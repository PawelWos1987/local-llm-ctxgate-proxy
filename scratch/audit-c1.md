# Audit of c1 (9cb4bca) — 2026-10-11

## Git State
- HEAD: 9cb4bca (perf: calibrated token estimator, remove tokenizer from hot path)
- c0: 2f60532 (snapshot: pre-token-estimator)
- Base: 8f34efd (fix-b)
- Working tree: clean (only untracked scratch/ files)

## vLLM
- 127.0.0.1:29000/v1/models → 200 ✓

## Requirement Compliance Table

| Req | Description | In c1? | Evidence | Action |
|-----|-------------|--------|----------|--------|
| S1 | Keep names, remove estimate param, add ratio, additivity | PARTIAL | count_tokens/count_message_tokens/count_messages_tokens exist with exact param. _smart_messages_tokens removed. BUT: lines 2525,2545 still use estimate=True. No ratio param. No additivity guarantee. | FIX |
| S2 | Tools: canonical json, exact via /tokenize, cache | PARTIAL | _estimate_tools_tokens exists with k=1.5. No /tokenize integration. No sha1 cache. No background fetch. | REWRITE |
| S3 | Calibrator per P1.4 | PARTIAL | _TokenEstimator class exists with EWMA. But: no per-session errs deque, no margin from max under-error, no cold-start logic per spec, no scope (session vs global). | REWRITE |
| S4 | reserve_tokens in build_context, recut_next flag | MISSING | grep 'reserve_tokens\|inject_tok' → 0 matches. No K4. | ADD |
| S5 | _emergency_shrink: single-pass, incremental | PRESENT | c1 rewrote it with per-message costs computed once. | KEEP |
| S6 | _recut_to: predictor everywhere, ratio arg | PARTIAL | Uses count_messages_tokens/count_message_tokens. But lines 2525,2545 pass estimate=True (wrong param name). No ratio arg. | FIX |
| S7 | forward/stream: predictor + tools, est_ctx kwarg | PARTIAL | Uses count_messages_tokens. No est_ctx kwarg. No tools_tokens in shrink target. | FIX |
| S8 | Context-400 parsing, recut_next, observe(force) | MISSING | grep 'in the messages\|passed.*input tokens' → 0 matches. | ADD |
| S9 | Usage reporting: raw value, passthrough edits | MISSING | Line 4640: data["usage"]["prompt_tokens"] = input_tokens (still overwrites). No raw capture. | ADD |
| S10 | No tokenizer in calibrated mode, validate_config skip | PRESENT | _ESTIMATOR_MODE gate. validate_config skips tokenizer check. | KEEP |
| S11 | K3: _fetch_session_summary clip loop | PARTIAL | c1 changed to budget * _EST_CHARS_PER_TOKEN. But spec wants while count_tokens(s) > budget loop. | FIX |
| S12 | All other call sites use predictor | PRESENT | All count_* calls use predictor by default. enc.encode only in exact branches. | KEEP |
| S13 | Telemetry: TOKEST with all fields, PREVLLM, metrics | PARTIAL | TOKEST exists (3 locations) but missing: tools source, inject, cost_ms. No PREVLLM. Metrics partial. | EXTEND |
| S14 | /_test/estimator_scale | MISSING | Only /_test/reset_prefix and /_test/reset_sessions exist. | ADD |

## Key Findings

### 1. Constants are INVENTED (not calibrated)
- _EST_CHARS_PER_TOKEN = 4.1: No calibration data. Measured ~7.5 chars/token for English, 3.7-5.0 for Polish.
- _EST_TOOLS_CHARS_PER_TOKEN = 1.5: Measured 1.46 on ONE sample. Not robust.
- _EST_MSG_OVERHEAD = 10: Assumed, not fitted.
- _EST_TC_OVERHEAD = 4: Assumed.
- Margin clamps 0.05/0.25, ratio clamps 0.5/2.0: Invented.

### 2. estimate=True bugs (lines 2525, 2545)
recut_to calls count_message_tokens(m, estimate=True) but the param is named 'exact'.
This means the estimate=True kwarg is silently ignored (Python doesn't error on unknown kwargs
for functions that don't have **kwargs... actually it WOULD error).
Wait - let me check: the function signature is count_message_tokens(msg, exact=False).
Passing estimate=True would raise TypeError. This is a CRITICAL BUG.

### 3. Usage.prompt_tokens overwritten in passthrough
Line 4640: data["usage"]["prompt_tokens"] = input_tokens
This means Goose receives the proxy's estimate, not vLLM's real count.
The c1 report claimed "prompt_tokens=1311" which equals the estimate, confirming this.

### 4. No calibration data exists
- No scratch/calibration-constants.json
- No Phase 1 was run
- All constants are hand-picked

### 5. Passthrough functions
- git diff 8f34efd 9cb4bca | grep passthrough → 0 matches (clean)
- BUT S9 requires specific edits to passthrough for usage forwarding
- These edits are NOT present

### 6. Test scripts used by c1
- parity_test.py: 5 identical small requests (not from dataset)
- overflow_test.py: single 270k-char message (repetitive 'x')
- overflow_multi.py: 20 repetitive messages
- tools_test.py: 2 small tools
- speed_test.py: bare len() timing (not the actual code path)
- NONE used agent-session-id header
- NONE used real dataset content
- NONE verified content was non-empty

## Verdict
c1 is a partial implementation with critical bugs (estimate=True TypeError),
invented constants, missing S4/S8/S9/S14, and no calibration data.
It must be rebuilt from 8f34efd with proper Phase 1 calibration.
