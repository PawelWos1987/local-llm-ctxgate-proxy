# MASTER_STATE

## Current Phase: 1b COMPLETE → Phase 3 (Ledger + Summarizer)

## Status
| Phase | Status | Gate |
|-------|--------|------|
| 0 | DONE | G-PREFIX baseline captured |
| 1 | DONE | 20/20 tests, G-PREFIX unchanged |
| 1b | DONE | 9/9 goldens, 10/10 F14, G-PREFIX unchanged |
| 2 | PENDING | - |
| 3 | PENDING | - |
| 4 | PENDING | - |
| 5 | PENDING | - |
| 6 | PENDING | - |
| 7 | PENDING | - |

ADDENDUM_1 active: Phase 1b + Phase 3 extension (INSTRUCTION ledger rows) + F13 fix.

## Key Facts
- Dev repo: /home/pawelw/ctxproxy-dev (git, baseline d412fe9, phase-1b 503e9ec). Live: /home/pawelw/ctxproxy (DO NOT EDIT/RESTART).
- Harness: tests/harness_window.py (pool=None, task_uuid=None, deterministic). Reference: tests/reference_prefix.json. G-PREFIX = compare first_diff sequence + mean stable ratio vs reference.
- Worker file: worker/worker.py. App: proxy/app.py.
- Test DB: ctxproxy_test. DSN: postgresql://postgres:11!!AdaMicPaw@127.0.0.1:5432/ctxproxy_test

## Phase 1b Changes (commit 503e9ec)
- O1: content_loop / reasoning_loop exit reasons
- O2: stream_options kept in retries, reasoning_chars_first
- O3: retry_length label
- O4: per-session tc_truncated warning
- F13: enqueue dedup fingerprints canonical envelope
- F14: _detect_loop multi-line rejection + consecutive-word test

## NEXT ACTION:
Phase 2: Seed dangling tool-call repair (_repair_dangling_tool_calls).

## Notes
- F11: _store_memory_actions/_update_working_memory NEUTRALIZED (fixed async-with, kept functions).
- Phase 1: all W1-W11 + A1-A2 done. 20/20 tests pass. G-PREFIX unchanged.
- test_deliverable.py requires live proxy on :9209 (pre-existing, not our concern).
