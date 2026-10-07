# DECISIONS
(log: date | decision | alternatives | why)

- 2026-10-07 | Use existing git repo in live tree as the dev baseline (no fresh git init) | fresh git init with single baseline commit | The live tree is already a clean git repo at d412fe9; cp -a carried .git over, so the dev tree has full history and a clean status. This preserves the "baseline" commit semantics without losing history.
- 2026-10-07 | Flatten cp -a nesting (ctxproxy-dev/ctxproxy -> ctxproxy-dev/) | keep nested layout | Plan expects /home/pawelw/ctxproxy-dev/proxy/app.py paths.
