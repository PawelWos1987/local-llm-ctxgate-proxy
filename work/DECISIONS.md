
## 2026-10-07 12:25 - ADDENDUM_1 merged
- Decision: Apply F13 fix + Phase 1b as retrofits (we're past Phase 1)
- Phase 3 INSTRUCTION kind: verify existing implementation
- Phase 5 context_slice: already done (CTXGATE_WORKER_SLICE_CHARS)
- Order: F13 first (small), then Phase 1b (large), then verify Phase 3 ext
- Rationale: ADDENDUM_1 says "where this addendum conflicts with MASTER_PLAN.md, this addendum wins"

## 2026-10-07 12:25 - ADDENDUM_1 Retrofit Complete
- **F13**: Fixed by using the same cleaned representation for both fingerprint and stored content. The old code compared sha256(raw[:5000]) with sha256(cleaned[:5000]) which never matched for >5000 char messages. Now both use cleaned[:5000].
- **Phase 1b**: _stream_truncated moved before NS-DIAG log. The original code used it in the log at line 4790 but assigned it at line 4799, causing UnboundLocalError on every stream. Fixed by moving the assignment up.
- **Phase 3 ext (INSTRUCTION)**: Removed keyword filter (addendum says "every real user message"), increased to 600 chars, added total length + sha1, dedupe by sha1 of full content.
- **S5/S6**: Verified passing.
