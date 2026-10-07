# DECISIONS
(log: date | decision | alternatives | why)

- 2026-10-07 | Use existing git repo in live tree as the dev baseline (no fresh git init) | fresh git init with single baseline commit | The live tree is already a clean git repo at d412fe9; cp -a carried .git over, so the dev tree has full history and a clean status. This preserves the "baseline" commit semantics without losing history.
- 2026-10-07 | Flatten cp -a nesting (ctxproxy-dev/ctxproxy -> ctxproxy-dev/) | keep nested layout | Plan expects /home/pawelw/ctxproxy-dev/proxy/app.py paths.

## 2026-10-07 11:43 - Phase 1 complete
- Decision: All W1-W11 + A1-A2 implemented. 20/20 tests pass. G-PREFIX unchanged.
- ADDENDUM_1 merged: Phase 1b (output-path integrity) + F13 fix + Phase 3 INSTRUCTION extension.
- Next: Phase 1b mock vLLM harness + characterization tests.
