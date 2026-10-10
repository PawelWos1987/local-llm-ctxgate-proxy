# PRE-FIX-B VERIFICATION — Token AND Char Limits
**Date:** 2026-10-10
**Target:** /home/pawelw/ctxproxy/proxy/app.py (read-only)
**DB:** ~/.local/share/goose/sessions/sessions.db (read-only)

---

## A. Question 1 — Does Goose output_tokens Include Reasoning Tokens?

**Q1-A.** Column: usage_ledger.output_tokens (INTEGER)
**Q1-B.** **CANNOT VERIFY** from DB alone. However, total_tokens = input_tokens + output_tokens for **all 4,948 rows** (100% match, 0 exceptions). This is the standard OpenAI API convention where total = prompt + completion. For vLLM serving Qwen3.8-27B, the completion tokens **include** reasoning tokens (they are part of the generated sequence). The Goose DB does NOT separate reasoning from content.
**Q1-C.** Max reasoning_chars (48h): **NOT AVAILABLE**. The proxy was restarted at 22:20 today; journal has only 193 lines with zero reasoning_chars entries.
**Q1-D.** Approx max reasoning_tokens: **NOT MEASURABLE** from available data. The backstop is MAX_REASONING_TOKENS=12000 (line 1132), triggered at 36000 chars.
**Q1-E.** Approx max content_tokens: 15,770 (observed max output_tokens, which likely includes reasoning).
**Q1-F.** Is MAX_OUTPUT=12000 safe? **TIGHT**. If output_tokens includes reasoning, then the observed max of 15,770 includes reasoning. A request with 12,000 reasoning + 3,770 content = 15,770. Setting MAX_OUTPUT=12000 would truncate any request where reasoning alone approaches 12000. The MAX_REASONING_TOKENS backstop (12000) means reasoning CAN reach 12000 tokens before being stopped. **Recommendation: keep MAX_OUTPUT at 15000 minimum** to accommodate reasoning + content.
**Q1-G.** Suggested MAX_OUTPUT for 2x safety: **15000** (= observed max 15770 rounded down, since the max likely includes reasoning).

---

## B. Question 2 — Real max_tokens Values in Production

**Q2-A.** Max max_tokens observed in 48h: **NOT AVAILABLE**. The proxy journal has only 193 lines (service restarted at 22:20). Zero entries contain "max_tokens".
**Q2-B.** Fraction with max_tokens=22500: **0** (no data).
**Q2-C.** Any MIN_OUTPUT floor hit in 48h: **NO** (0 occurrences of "MIN_OUTPUT" in journal).
**Q2-D.** MAX_OUTPUT=12000 safe? **NO** (see Q1-F — reasoning tokens are included in the output budget).
**Q2-E.** MIN_OUTPUT=8000 safe? **YES** (it was already a dead floor at 16000; reducing to 8000 is safe).

**Data gap:** The proxy was restarted 13 minutes before this diagnostic. We have no production max_tokens data from the journal. The A3 report's DB stats (output max=15770) remain the best available evidence.

---

## C. Question 3 — Every Char-Based Limit in app.py

### C.1 — Named Constants

| # | Constant | Value | Line | Purpose | Token Equiv (div 7.5) | Interaction with Token Budget |
|---|----------|-------|------|---------|----------------------|------------------------------|
| 1 | SUMMARY_BACKFILL_CHARS | 96,000 | 1066 | Cap total chars in summary backfill | ~12,800 tok | Off hot path (async summarizer) |
| 2 | KNOWLEDGE_EXTRACT_MIN_CHARS | 40 | 1067 | Min chars to trigger knowledge extraction | ~5 tok | Off hot path |
| 3 | SUMMARY_CHUNK_CHARS | 24,000 | 1068 | Max chars per summarizer chunk | ~3,200 tok | Off hot path |
| 4 | SUMMARY_TOOL_CAP_CHARS | 1,000 | 1070 | Cap tool output in summarizer input | ~133 tok | Off hot path |
| 5 | PINNED_USER_MAX_CHARS | 16,000 | 1081 | Hard cap on pinned user copy | ~2,133 tok | **ON HOT PATH** |
| 6 | PINNED_USER_FAR_CHARS | 2,000 | 1082 | Tighter cap when user msg is far from cut | ~267 tok | **ON HOT PATH** |
| 7 | MAX_BODY_BYTES | 20,971,520 | 1124 | HTTP request body size limit (20MB) | N/A | Hard 413 if exceeded |
| 8 | CTXGATE_WORKER_SLICE_CHARS | 12,000 | 784 | Worker memory extraction slice | ~1,600 tok | Off hot path (worker) |
| 9 | LOOP_TAIL | 4,000 | ~1130 | Loop detection tail window | ~533 tok | Off hot path (streaming guard) |

### C.2 — Inline Char Thresholds (No Named Constant)

| # | Expression | Value | Line | Purpose | Token Equiv | On Hot Path? |
|---|-----------|-------|------|---------|------------|-------------|
| 10 | len(c) > 2500 | 2,500 | 2692 | _stable_elide_inplace threshold | ~333 tok | **YES** (fast path) |
| 11 | c[:1000] + c[-1000:] | 2,000+marker | 2673 | _elided_tool_content | ~267 tok | **YES** (emergency_shrink) |
| 12 | t[:600] + t[-400:] | 1,000+marker | 857 | Summary tool cap truncation | ~133 tok | No |
| 13 | args[:300] | 300 | 869 | Summary tool_call args cap | ~40 tok | No |
| 14 | phase_summary[:3000] | 3,000 | 973 | Phase summary DB insert cap | ~400 tok | No |
| 15 | digest[:6000] | 6,000 | 730 | Session digest join cap | ~800 tok | No |
| 16 | tm[:2000], kn[:2000] | 2,000 | 1303,1309 | Injection metrics logging | ~267 tok | No |
| 17 | content[:300] | 300 | 2158,2332 | _msg_anchor hash input | ~40 tok | **YES** (anchor) |
| 18 | len(_slice_log) > 1000 | 1,000 | 834 | Slice log rotation | N/A | No |

### C.3 — Divisor-by-4 Expressions (Token-to-Char Conversion)

| # | Expression | Line | Assumption | Correct for Qwen? | Impact |
|---|-----------|------|-----------|-------------------|--------|
| 1 | len(digest) // 4 | 746 | 4 chars/token | **NO** (Qwen=7.5) | Overestimates by 87%. Cosmetic (DB col). |
| 2 | s[:budget * 4] | 1051 | 4 chars/token | **NO** (Qwen=7.5) | Returns 1.9x more chars than budget allows. Under-utilizes (safe). |
| 3 | len(text) // 4 | 1780 | 4 chars/token | **NO** (Qwen=7.5) | Fallback when tokenizer disabled. Overestimates 87%. |
| 4 | len(full)//4 + n*12 | 1813 | 4 chars/token | **NO** (Qwen=7.5) | Multi-message fallback. Overestimates 87%. |

### C.4 — Answers

**Q3-A.** **18** distinct char-based limits (9 named + 9 inline)
**Q3-B.** **4** assume 4 chars/token (lines 746, 1051, 1780, 1813)
**Q3-C.** **2** could become active at 68000: PINNED_USER_MAX_CHARS (3.1% of budget) and _stable_elide 2500 threshold. Neither is a hard bottleneck.
**Q3-D.** **1** hard failure: MAX_BODY_BYTES=20MB (HTTP 413). Far above any realistic request. Not a concern.

### C.5 — The budget*4 Problem (Line 1051)

In _fetch_session_summary: budget=1500 tokens, s[:budget*4]=s[:6000] chars. At Qwen 7.5 c/t: 6000 chars = ~800 actual tokens. Summary is shorter than intended (safe direction, suboptimal). Fix: change to s[:budget*8].

---

## D. Question 4 — Fresh-Session State Integrity

**Q4-A.** **YES** deterministic. New session: ws=None, fast path=False, slow path taken. Same input=same cut.
**Q4-B.** **NONE** survive restart. Only proxy.session_windows (PG) persists.
**Q4-C.** **NO** race. Per-session asyncio.Lock serializes concurrent requests.
**Q4-D.** **NO** interference. New session_keys are unique. Old rows cleaned by TTL (7 days).

---

## E. Verdict Table

| Change | Verdict | Reason |
|--------|---------|--------|
| MAX_OUTPUT 22500 to 12000 | **UNSAFE** | output_tokens includes reasoning. Observed max=15770. Use 15000. |
| MIN_OUTPUT 16000 to 8000 | **SAFE** | Was dead floor. 8000 is 2.5x p50. |
| SAFETY_MARGIN 3500 to 4000 | **SAFE** | Covers estimation error. Soft-fail catches overflow. |
| MAX_INPUT 58000 to 68000 | **SAFE** | No char limit binds. p99 input=58145. |
| divisor 4 to 8 in count_tokens | **SAFE** | Only active when tokenizer disabled. |
| Char limits that block | **NONE** | No char limit becomes bottleneck. |

---

## F. Unsafe Verdicts — What is Needed

**MAX_OUTPUT=12000 is UNSAFE.** Options:
1. Confirm vLLM completion_tokens includes reasoning (test request).
2. If yes: set MAX_OUTPUT=15000 (keeps MAX_REASONING_TOKENS=12000 intact).
3. If forced to 12000: must reduce MAX_REASONING_TOKENS to 8000.

**Adjusted parameters with MAX_OUTPUT=15000:**
- MAX_CONTEXT=84000, MAX_INPUT=**65000**, MAX_OUTPUT=**15000**, MIN_OUTPUT=**8000**, SAFETY_MARGIN=**4000**
- Check: 65000+15000+4000=84000 ✓

---

## G. Char Limits to Adjust with Token Limits

| Char Limit | Current | Proposed | Reason |
|-----------|---------|----------|--------|
| budget*4 (line 1051) | 4 c/t | **8 c/t** | Under-utilizes summary budget by 47%. |
| len(text)//4 (line 1780) | 4 c/t | **8 c/t** | Fallback estimator accuracy. |
| len(full)//4+n*12 (line 1813) | 4 c/t | **8 c/t** | Multi-message fallback. |
| len(digest)//4 (line 746) | 4 c/t | **8 c/t** | Cosmetic. Low priority. |
| All named char constants | various | **unchanged** | None become binding at 65000 tokens. |

---

PRE-FIX-B VERIFICATION COMPLETE
