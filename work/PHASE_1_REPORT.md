# Phase 1 Report: Zero-Risk Correctness Fixes

## Changes

### worker.py
| Item | Description | Status |
|------|-------------|--------|
| W1 | Atomic claim: single UPDATE...WHERE id IN (SELECT...FOR UPDATE SKIP LOCKED) + inflight_tasks set + _claim_lock | DONE |
| W2 | _norm: NFKC + casefold, re.UNICODE, hash fallback for empty | DONE |
| W3 | NEW collision: near-dup -> touch, different -> SUPERSEDE (old row stays) | DONE |
| W4 | Deterministic grounding: path/identifier tokens must appear in source | DONE |
| W5 | task_desc: first user event (seq lowest, <=1500 chars) + task name | DONE |
| W6 | WM: write only when changed=true and state non-empty | DONE |
| W7 | Remove source_event_id from schema; TEMP=0.1 | DONE |
| W8 | Heartbeat refresh claimed_at every 30s; stale threshold 600s | DONE |
| W9 | CONSUMERS=4; token-bucket RPM; honor Retry-After | DONE |
| W10 | Hourly retry of failed jobs with attempts < hard cap | DONE |
| W11 | Fix _atomic_write logging; rename 4B/LM Studio wording | DONE |

### app.py
| Item | Description | Status |
|------|-------------|--------|
| A1 | Remove stuck-job failing logic in _memory_worker_loop (worker owns recovery) | DONE |
| A2 | Neutralize dead _store_memory_actions/_update_working_memory (fix async-with, keep functions) | DONE |

## Tests
- tests/test_phase1.py: 20/20 PASS
  - 50 concurrent claims -> no duplicates
  - Per-task ordering: second claim skips in-flight task
  - Polish titles: distinct non-empty keys (incl. emoji)
  - SUPERSEDE: old row superseded, new row active
  - Grounding: drops only ungrounded entries
  - WM: never overwritten with empty

## Gate
- G-PREFIX: reference_prefix.json UNCHANGED (Phase 1 does not touch request path)
- py_compile: both files OK

## Risks
- None identified. All changes are additive/isolated to worker and dead code.
