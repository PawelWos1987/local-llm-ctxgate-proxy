# MASTER_STATE

Date: 2026-07-10 (session clock 2026-10-07)
Phase: 0 (Baseline + safety harness)
Last green commit: d412fe9 (baseline; dev tree clean, byte-identical to live)

## Status
| Item | Status |
|---|---|
| Dev copy /home/pawelw/ctxproxy-dev | DONE (cp -a, flattened, git repo intact) |
| MASTER_PLAN.md saved | DONE |
| BASELINE.md (hashes) | IN PROGRESS |
| Re-verify F1-F12 -> PHASE_0_REPORT.md | NOT STARTED |
| Baseline metrics (prefix ratios, re-cut spacing, vLLM cache, latencies) | NOT STARTED |
| tests/harness_window.py | NOT STARTED |
| tests/reference_prefix.json (G-PREFIX reference) | NOT STARTED |

## Key facts
- Live tree: /home/pawelw/ctxproxy (git, HEAD d412fe9, clean). NEVER edit/restart.
- Dev tree: /home/pawelw/ctxproxy-dev (git, same HEAD, clean).
- app.py 5759 lines, worker.py 965 lines (live == dev, sha256 recorded in BASELINE.md).
- Work files in /home/pawelw/ctxproxy-dev/work/.

NEXT ACTION: write BASELINE.md (hashes done: app.py 8d033db1..., worker.py 314c02ab...), then grep-verify F1-F12 in dev copies.
