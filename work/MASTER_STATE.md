# MASTER STATE
Updated: 2026-10-07 11:43

## Current Phase
Phase 1b (ADDENDUM_1: output-path integrity + F13 enqueue dedup)

## Status
| Phase | Status | Tag |
|-------|--------|-----|
| 0 | DONE | phase-0-ok |
| 1 | DONE | phase-1-ok |
| 1b | IN PROGRESS | - |
| 2 | PENDING | - |
| 3 | PENDING | - |
| 4 | PENDING | - |
| 5 | PENDING | - |
| 6 | PENDING | - |
| 7 | PENDING | - |

ADDENDUM_1 active: Phase 1b + Phase 3 extension (INSTRUCTION ledger rows) + F13 fix.

## Key Facts
- Dev repo: /home/pawelw/ctxproxy-dev (git, baseline d412fe9). Live: /home/pawelw/ctxproxy (DO NOT EDIT/RESTART).
- Harness: tests/harness_window.py (pool=None, task_uuid=None, deterministic). Reference: tests/reference_prefix.json. G-PREFIX = compare first_diff sequence + mean stable ratio vs reference.
- Worker file: worker/worker.py. App: proxy/app.py.
- Test DB: ctxproxy_test (pg_dump --schema-only of live). DSN: postgresql://postgres:11!!AdaMicPaw@127.0.0.1:5432/ctxproxy_test

## NEXT ACTION:
Phase 1b: Mock vLLM harness (scripted SSE server) + characterization tests for exit_reason=ok path. Then F13 fix (enqueue dedup fingerprint).

## Notes
- F11: _store_memory_actions/_update_working_memory NEUTRALIZED (fixed async-with, kept functions).
- Phase 1: all W1-W11 + A1-A2 done. 20/20 tests pass. G-PREFIX unchanged.
