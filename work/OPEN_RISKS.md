# OPEN_RISKS.md
Last updated: 2026-10-07 12:45

1. **test_phase1.py under pytest runner**: the 6 async tests require `pytest-asyncio`
   (not installed in the dev env). They pass when run directly with a plain asyncio runner.
   UNVERIFIED under the shared pytest runner. Not a code defect.
2. **test_deliverable.py**: requires a live server; skipped (would hit the protected 9201).
   UNVERIFIED in CI.
3. **Stable-ratio baseline shift**: Phase 0 measured 0.9610 on a 51KB session (empty tool
   content); Phase 6 measures 0.8933 on the full 573KB session. Same-data comparison
   (current vs reference_real_session.json) is 0.00 pp. The absolute drop is a data-fidelity
   artifact, not a regression. UNVERIFIED against the true 15M-token production session
   (we only have the 222-msg 20261006_31 copy).
4. **Live services not restarted**: all changes are in the dev tree. Production behavior
   UNVERIFIED until DEPLOY.md is applied and post-deploy checks pass.
5. **Mistral call budget**: Phase 3 makes the ledger independent of Mistral, so a Mistral
   outage no longer blocks the watermark. The optional LLM rollup of old phases is off by
   default (deterministic digest used). No new per-request LLM calls added.
