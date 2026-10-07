# ADDENDUM 1 to MASTER_PLAN.md (output-path integrity + enqueue dedup)

Save as `/home/pawelw/ctxproxy-dev/work/ADDENDUM_1.md`. Add one line to `MASTER_STATE.md`: "ADDENDUM_1 active: Phase 1b + Phase 3 extension".
Where this addendum conflicts with MASTER_PLAN.md, this addendum wins. All MASTER_PLAN section 2 invariants and section 4 rules still apply.
Line numbers are from the analysed copy of app.py (5759 lines). Re-verify every finding on the LIVE file first
(`VERIFIED` / `NOT REPRODUCED` / `CHANGED` + evidence in `PHASE_1B_REPORT.md`).

## New findings (from live logs of session 20261007_1 + code reading)

**F13. Enqueue dedup never matches long user messages.** `_enqueue_memory_job` (def ~4521) compares
`sha256(last_event.content[:5000])` with `sha256(cleaned[:5000])`. For messages >5000 chars the STORED content is
`head(3000) + "\n[...N chars omitted...\n" + tail(1500)` (~4.5k chars), so the two hashes can never be equal. The new session starts with a
very long user prompt, and the log shows a memory job for EVERY turn (seq 1..8 in ~2 minutes), versus 6 jobs in 12 h in the old session whose
user messages were short. Effects: one duplicate job per turn (up to 3-4 Mistral calls each), duplicate event rows, repeated memory "refreshes".
Also, the worker only ever sees the first 2500 chars of such a prompt, so the middle of a long instruction never becomes memory.

**F14. A content loop on the FIRST attempt is silent.** Around lines 4081-4090 the loop detector sets `loop_in_content=True` and breaks the
upstream stream. The only code that turns `loop_in_content` into `exit_reason="loop"` is inside the non-thinking RETRY block (~4382).
On the first attempt `exit_reason` stays `"ok"` and `finish_reason` stays `"stop"`, so the client gets cut-off content as a normal, complete answer
and any tool call the model would have emitted after that text is lost. For Goose that reads as "turn finished", so the session waits for the human.
Detector facts (`_detect_loop`, ~1407): periodic test p>=20 repeated 4x, and a sentence test where a normalised line/sentence of >=30 chars
repeats >=6 times inside the last 4000 chars (it splits on newlines, so repeated long lines in tables, logs or generated code can match).
Unmeasured false-positive rate.

**F15. Token accounting is zero after a loop retry.** Both retry blocks (~4136 and ~4233) delete `stream_options`, so vLLM sends no usage chunk,
`seg_output_tokens` stays 0 and the log shows `exit=loop_recovered ... total_out=0` even though a complete tool call was emitted.
Impact: wrong `usage.completion_tokens` reported to Goose, wrong `tokens_out_total`, and the `CTXGATE_MAX_TOTAL_OUTPUT` budget math is not reduced.
No data loss. Also `reasoning_chars` is reset to 0 on retry, and `truncated=` in NS-DIAG is just `exit_reason != "ok"`, so a recovered loop
is counted in `output_truncated_total`.

**F16. The "Loop recovery" retry (~4225-4390) is a single segment with no continuation.** If it ends with `finish=length`, nothing continues it
and `exit_reason` still becomes `loop_recovered`. (The other recovery path, `finish=length` with empty content at ~4121, re-enters the main loop
and does get continuations. Do not confuse them.)

**F17. Tool-call deltas are forwarded to the client before completeness is known.** A call cut by `max_tokens` has already been streamed as partial
JSON; the proxy can only flag it afterwards (`tool_call_truncated`). Design limitation. Do NOT add buffering or change forwarding of the normal path.
Measure and report only.

**F18. Retry sampling is aggressive for code/tool arguments.** The retry uses thinking off, `presence_penalty=1.5`, `temperature=0.7`, `top_p=0.8`.
That can distort identifiers and repeated structures in file-write tool calls. Not a bug; log it in `OPEN_RISKS.md`. Do not change defaults without evidence.

**Evidence that abandoned reasoning is not persisted:** after the 36,009-char reasoning backstop turn, context grew only 15,428 -> 15,920 tokens
(+492 for the assistant tool call + tool result). Reasoning is not re-sent in history.

## Phase 1b: Output-path integrity (run right after Phase 1, before Phase 2)

Rule: the `exit_reason == "ok"` path must stay byte-identical on the wire. Characterization tests first, fixes second.

1. **Mock vLLM harness.** A small scripted SSE server (set `CTXGATE_VLLM_URL` to it) so you can run the dev proxy without touching the live vLLM.
   Capture current wire output (raw SSE bytes to the client, plus the final NS-DIAG line) as golden files for these scenarios:
   (a) normal tool call; (b) text hitting `finish=length`, then continuation with a seam; (c) reasoning backstop with no content, then a retry that
   returns a tool call; (d) reasoning loop caught by `_detect_loop`; (e) content loop on first attempt, once with no tool call yet and once after
   a complete tool call; (f) tool call truncated by `finish=length`; (g) stream interrupted mid-content; (h) retry that itself ends with `finish=length`.
   Live-vLLM canary budget: at most 3 requests with `max_tokens<=16`, only to confirm that `stream_options.include_usage` is accepted.
2. **False-positive corpus for F14.** Replay every historical assistant message from the Goose DB copy through the detector at the same
   256-char cadence the proxy uses. Report how many messages that completed normally would have been cut. Do not change thresholds unless this
   corpus shows false positives; if it does, add the narrowest exclusion that fixes them (for example, ignore lines inside code fences or table separator
   rows) and re-run the corpus.
3. **Fixes (each behind its own env flag, default on after tests pass):**
   - O1 (F14): first-attempt content loop sets `exit_reason="loop"`, `finish_reason="length"`, increments a dedicated metric, and logs period, chars
     and whether a tool call was pending. Do NOT retry on content loops (partial text is already on the wire; a retry would duplicate it).
   - O2 (F15): keep `stream_options={"include_usage": true}` in both retry bodies if the canary confirms vLLM accepts it; otherwise estimate
     completion tokens from emitted content and tool-call arguments and report that estimate. Preserve first-attempt reasoning chars in the diag
     (`reasoning_chars_first`). Count a recovered loop that ends with a complete tool call as success, not as truncated.
   - O3 (F16): label a retry segment that ends with `finish=length` as `exit_reason="retry_length"` (not `loop_recovered`). Add continuation to that
     path only if it can reuse the existing continuation code without duplicating it and all golden tests (a)-(h) still pass; otherwise label only.
   - O4 (F17): add counters (`tool_call_truncated` by session, last tool name, `max_tokens` at the time) so the risk is visible in the dashboard. No behavior change.
4. **Gate:** golden tests (a),(b) byte-identical before/after; (c)-(h) match their intended new behavior; no change to `build_context` output
   (G-PREFIX from MASTER_PLAN Phase 0 unchanged); latency of the ok path unchanged.

## Phase 1 addition: fix F13 (enqueue dedup)

- Fingerprint the SAME representation on both sides: compute `fp = sha256(envelope(cleaned))` where `envelope()` is the exact function that builds the stored
  string (head+tail+marker), and compare with the last stored event's own fingerprint (store it in `events.meta` or compute it from the stored content with the
  same function). Test: a 20k-char user message enqueued twice yields one job; two different 20k messages with the same first 3000 chars yield two jobs.
- Re-check the `<30 chars` filter against real data; keep it unless it drops real instructions.

## Phase 3 extension: record user instructions deterministically

The extractor also writes a ledger row `kind='INSTRUCTION'` for every real user message that is dropped from the window (not tool results, not
`<turn-context>`), with the first 600 chars, total length and sha1. This keeps "what the user asked" recoverable even when the pinned copy is replaced
by a newer user message. Dedupe by sha1. Long instructions (>5000 chars) must also reach the worker in chunks of at most `CTXGATE_WORKER_SLICE_CHARS`
via the Phase 5 `context_slice` path instead of being cut to 2500 chars.

## Acceptance additions (S5, S6)

- S5 No silent truncation: in the Phase 6 replay and in scenarios (a)-(h), every cut-off output carries `finish_reason=length` and a non-"ok" exit reason.
- S6 One memory job per distinct user message (F13 test), and zero memory jobs created by identical re-sends.
