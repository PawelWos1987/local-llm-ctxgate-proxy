# AUX-ISOLATION VALIDATION REPORT
Date: 2026-10-10 17:20 UTC+2
Proxy: /home/pawelw/ctxproxy/proxy/app.py
Test infra: fake OpenAI-compatible LLM (127.0.0.1:19204), sandbox proxy (127.0.0.1:19203)
DB: ctxproxy_sandbox (writes), ctxproxy (read-only)

## 1. Results Table

| Check | Result | Evidence |
|-------|--------|----------|
| Static: git diff (2 hunks only) | PASS | 2 hunks: removed session_compactions.pop(sk), else->elif len(built)>=3 |
| Static: py_compile | PASS | COMPILE_OK |
| Static: --check-config | PASS | "OK: configuration valid" |
| Static: app.py.bak untracked | PASS | Already in .gitignore (proxy/app.py.bak*), not staged |
| Scenario A: trim + aux (FIXED) | PASS | dropped=1, seed=0, h3pop=0 after 3 aux + next over-limit |
| Scenario B: restart + resume (FIXED) | PASS | No re-summarization, no state loss |
| Scenario C: negative control (ORIGINAL) | PASS (defect reproduced) | seed=1 after aux, seed=2 after next over-limit (flap) |
| R1: under-limit stale state | PASS | h3pop=0, seed=0 after compact+over-limit cycle |
| R2: 3+ msgs diff system | PASS (no corruption) | corrupt=False, flap=True (expected: seed updates on new system) |
| R3: prod events pollution | PASS | title_like=0/32, no title-gen prompts in prod events |
| Phase 4: verify_aux_isolation.sh | PASS | 9/9 checks passed |

## 2. CLI Isolation

CLI skipped: no goose CLI commands were executed. Real config.yaml mtime=1791597529 (03:58, pre-test). No sessions.db exists. Zero risk to Desktop session.

## 3. Unified Diff (app.py)

```diff
0
diff --git a/proxy/app.py b/proxy/app.py
index 168cda2..3cde83d 100644
--- a/proxy/app.py
+++ b/proxy/app.py
@@ -2904,8 +2904,6 @@ async def build_context(request_messages: list, task_uuid: str = None, session_k
         total = count_messages_tokens(messages)
         log.info("Context: %d messages, %d tokens (limit %d)", len(messages), total, MAX_INPUT)
         if total <= MAX_INPUT - _note_n:
-            if sk:
-                session_compactions.pop(sk, None)
             return messages
         target = _trim_target()
         # Pre-recut elision: shrink tool bodies up to the newest-4 boundary so
@@ -3900,7 +3898,7 @@ async def chat_completions(request: Request):
             if len(built) >= 3:
                 session_seeds[session_key] = [dict(m) for m in built[:3]]
                 log.info("Seed frozen session=%s (3 msgs)", session_key)
-        else:
+        elif len(built) >= 3:
             frozen = session_seeds[session_key]
             for i, fm in enumerate(frozen):
                 if i < len(built):

```

## 4. Notes

- Scenario C confirms the defect: original code shows SEED CHANGED on aux requests (seed=1 after aux, seed=2 after next over-limit), proving the else-branch was incorrectly updating seeds for non-main requests.
- R2 flap=True is expected behavior: a genuinely different system prompt SHOULD update the seed. This is not corruption (h3pop=0, no state loss).
- session_windows is empty in all scenarios because window persistence only occurs on summary completion, not on trim alone. The in-memory state (session_compactions) is what matters for the fix.
- Ceiling=100 confirmed in startup log. CTXGATE_COMPACTION_NOTE="" set in test env to avoid note-token subtraction.

SAFE TO RESTART
