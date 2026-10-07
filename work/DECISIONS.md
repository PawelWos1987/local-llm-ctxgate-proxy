# DECISIONS
(log: date | decision | alternatives | why)

- 2026-10-07 | Use existing git repo in live tree as the dev baseline (no fresh git init) | fresh git init with single baseline commit | The live tree is already a clean git repo at d412fe9; cp -a carried .git over, so the dev tree has full history and a clean status. This preserves the "baseline" commit semantics without losing history.
- 2026-10-07 | Flatten cp -a nesting (ctxproxy-dev/ctxproxy -> ctxproxy-dev/) | keep nested layout | Plan expects /home/pawelw/ctxproxy-dev/proxy/app.py paths.

## 2026-10-07 11:43 - Phase 1 complete
- Decision: All W1-W11 + A1-A2 implemented. 20/20 tests pass. G-PREFIX unchanged.
- ADDENDUM_1 merged: Phase 1b (output-path integrity) + F13 fix + Phase 3 INSTRUCTION extension.
- Next: Phase 1b mock vLLM harness + characterization tests.

## 2026-10-07 Phase 1b Decisions
- **D1**: content_loop/reasoning_loop as separate exit reasons (not just "loop"). Rationale: ADDENDUM_1 O1 requires distinguishable labels for observability.
- **D2**: stream_options kept in retry bodies (was deleted). Rationale: O2 requires usage chunk visibility. vLLM accepts stream_options on all requests.
- **D3**: retry_length as new exit reason for retry segments hitting max_tokens. Rationale: O3.
- **D4**: _detect_loop: reject multi-line periods (unit.count newline > 1). Rationale: F14 false positives on tables/code/SQL. True loops are continuous text.
- **D5**: Consecutive-word test (same word 10+ in a row) instead of frequency-based. Rationale: F14 - frequency catches legitimate repeated words in structured content.
- **D6**: F13 dedup: fingerprint the canonical envelope (head+tail+marker) on both sides. Rationale: F13 - stored content is the envelope, so fingerprint must match.
- **D7**: reasoning_chars_first tracked separately from reasoning_chars (which gets reset on retry). Rationale: O2 - preserve first-attempt visibility.
