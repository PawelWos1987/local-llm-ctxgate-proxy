# MASTER_STATE

## Current Phase: 2 COMPLETE → Phase 3 (Ledger + Summarizer)

## Status
| Phase | Status | Gate |
|-------|--------|------|
| 0 | DONE | G-PREFIX baseline captured |
| 1 | DONE | 20/20 tests, G-PREFIX unchanged |
| 1b | DONE | 9/9 goldens, 10/10 F14, G-PREFIX unchanged |
| 2 | DONE | 13/13 tests, G-PREFIX unchanged |
| 3 | PENDING | - |
| 4 | PENDING | - |
| 5 | PENDING | - |
| 6 | PENDING | - |
| 7 | PENDING | - |

ADDENDUM_1 active: Phase 1b + Phase 3 extension (INSTRUCTION ledger rows) + F13 fix.

## Key Facts
- Dev repo: /home/pawelw/ctxproxy-dev (git, baseline d412fe9, phase-2-ok). Live: /home/pawelw/ctxproxy (DO NOT EDIT/RESTART).
- Harness: tests/harness_window.py. Reference: tests/reference_prefix.json.
- Worker: worker/worker.py. App: proxy/app.py.
- Test DB: ctxproxy_test. DSN: postgresql://postgres:11!!AdaMicPaw@127.0.0.1:5432/ctxproxy_test

## NEXT ACTION:
Phase 3: Durable deterministic ledger + summarizer fixes.
1. Migration: proxy.session_ledger table + phase_summaries columns
2. Deterministic extractor (tool calls → ARTIFACT/TEST_RESULT/FAILURE)
3. Summarizer: idempotent phases, watermark independence, better prompt
4. _build_session_digest (replaces root step)
5. ADDENDUM_1: INSTRUCTION ledger rows for user messages

## Notes
- Phase 2: _repair_dangling_tool_calls applied before fingerprint+vLLM body.
  Invariant warning still fires (runs in build_context before repair) - cosmetic.
- F14: _detect_loop fixed (multi-line rejection + consecutive-word test).
- F13: dedup fingerprints canonical envelope.
