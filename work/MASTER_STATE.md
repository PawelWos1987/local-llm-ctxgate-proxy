# MASTER STATE
Last updated: 2026-10-07 12:25

## Current Phase: RETROFIT COMPLETE (ADDENDUM_1)
Status: ALL RETROFITS DONE

## ADDENDUM_1 active: Phase 1b + Phase 3 extension (INSTRUCTION) + F13 fix

## Status Table
| Phase | Status | Tag |
|-------|--------|-----|
| 0 | DONE | phase-0-ok |
| 1 | DONE | phase-1-ok |
| 1b | DONE (retrofit) | phase-1b-ok |
| 2 | DONE | phase-2-ok |
| 3 | DONE | phase-3-ok |
| 3-ext | DONE (INSTRUCTION kind fixed) | phase-1b-ok |
| 4 | DONE | phase-4-ok |
| 5 | DONE | phase-5-ok |
| 6 | PENDING | - |
| 7 | PENDING | - |

## Last Green Commit
9516c25 Phase 1b: mock vLLM harness + golden tests (a)-(h) + O1-O4 fixes (tag: phase-1b-ok)

## ADDENDUM_1 Retrofit Results
- **F13 (enqueue dedup)**: PASS - 1 job for 2 identical >5000-char sends. Fixed by fingerprinting the same representation (cleaned) on both sides.
- **Phase 1b (golden harness)**: PASS - 9/9 scenarios, correct exit reasons, NS-DIAG lines present.
  - O1: content_loop exit_reason on first-attempt loop
  - O2: stream_options in retry bodies + reasoning_chars_first
  - O3: retry_length label
  - O4: tool_call_truncated counter
  - _stream_truncated: moved before NS-DIAG log (was UnboundLocalError)
- **Phase 3 ext (INSTRUCTION)**: PASS - every real user msg (not turn-context), 600 chars + total len + sha1, dedupe by sha1.
- **S5 (no silent truncation)**: PASS - all 8 truncated scenarios have finish_reason=length + non-ok exit.
- **S6 (one job per distinct msg)**: PASS - F13 test confirms.

## NEXT ACTION:
Phase 6: End-to-end validation (replay real session)

## Key Files
- proxy/app.py: ~6130 lines (dev copy)
- worker/worker.py: ~970 lines (dev copy)
- work/ADDENDUM_1.md: F13-F18 + Phase 1b + Phase 3 ext
- tests/golden_p1b.py: 9-scenario golden harness
- tests/test_f13_dedup.py: F13 dedup test
