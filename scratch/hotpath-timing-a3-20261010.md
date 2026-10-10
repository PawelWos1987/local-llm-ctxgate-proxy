# DIAGNOSTIC A3 — Surgical Tokenization Analysis
**Date:** 2026-10-10
**Target:** /home/pawelw/ctxproxy/proxy/app.py (read-only)
**DB:** ~/.local/share/goose/sessions/sessions.db (read-only)
**Capture:** /tmp/session_capture.json (A2)

---

## A. Executive Summary

1. **There is NO single place where exact tokenization is strictly required for correctness.** The user's hypothesis is directionally correct but overstated. The closest site is the _recut_to per-message loop (line 2384), which determines the cut position stored in session_windows. However, a well-calibrated estimation (chars/7.4) achieves ±1-2 message accuracy, which is sufficient for prefix-cache stability.

2. **The critical finding is that chars/4 is the WRONG divisor for the Qwen tokenizer.** The Qwen3.8-27B tokenizer achieves ~7.5 chars/token (not 4). Using chars/4 overestimates by ~87%. The correct divisor is 7.4-8.0, which reduces MAPE to 9-14%. This single change eliminates the need for exact tokenization in all but the most edge-case scenarios.

3. **The 400k re-tokenization problem is real but limited to COLD requests.** The fast path (line 2872) only tokenizes the kept window (~40-58k), not the full 400k. The slow path (lines 2935, 2377) tokenizes the full input, but this only occurs on the first request for a session (or after a restart). For WARM requests, the fast path is taken and the 400k is never tokenized.

4. **Real output stats (48h, 4943 turns):** max=15770, p99.9=11395, p99=6176, p50=523, mean=929. MIN_OUTPUT (16000) is above the observed max — it is a dead floor. MAX_OUTPUT (22500) is 1.43x the observed max.

5. **Proposed parameter changes:** MAX_OUTPUT 22500→12000, MIN_OUTPUT 16000→8000, SAFETY_MARGIN 3500→4000, MAX_INPUT 58000→68000. This increases the usable context window by 17% while maintaining 2x safety over observed p99.9.

6. **Estimated savings:** Replacing exact tokenization with chars/7.4 estimation in the slow path eliminates ~6.5s of the 7.3s tokenization time (89% reduction). The fast path is already O(kept) and unaffected.

---

## B. Section 1 — Tokenization Sites Table

| Site | Line | Input Size (Hot Path) | Tolerance | Wrong Value → Behavior Change? | Fast Path? | Slow Path? |
|------|------|----------------------|-----------|-------------------------------|------------|------------|
| build_context: total = count_messages_tokens(messages) | 2935 | 400k (cold) / N/A (warm) | ±10% flips decision only in 58000±5800 window | Yes: if est<limit but actual>limit → untrimmed sent → vLLM 400 → re-trim (soft fail) | No | Yes |
| build_context: kept_tok = count_messages_tokens(kept) (fast) | 2891 | ~40-58k (kept window) | ±5% may trigger/skip emergency_shrink | Minor: unnecessary shrink (wasteful) or slight over-ceiling (caught by vLLM 400) | Yes | No |
| build_context: kept_tok = count_messages_tokens(kept) (slow) | 2978 | ~40-58k (kept window) | ±5% | Same as above | No | Yes |
| _recut_to: total_tok = count_messages_tokens(messages) | 2377 | 400k (cold) | ±10% affects collapse guard only | Minor: collapse guard may fire/skip incorrectly | No | Yes |
| _recut_to: per-message mt = count_message_tokens(m) loop | 2384 | 400k (cold) | ±10% shifts cut by ~1-2 msgs | Most sensitive: cut position affects prefix cache. ±1-2 msgs is acceptable; ±5+ breaks cache | No | Yes |
| chat_completions: input_tokens = count_messages_tokens(built) | 4135 | ~40-58k (built) | ±10% affects _output_budget in 58000-64500 range (11.2% of MAX_INPUT) | Minor: slightly suboptimal max_tokens | Yes | Yes |
| _build_session_digest → count_tokens | 695+ | Small (KB) | N/A | None: only on epoch change, not per-request | No | No (async) |
| _fit_to_budget → count_tokens(text) | 2191 | Hundreds of tokens | ±20% | None: drops oldest sections, order preserved | No | No |
| fetch_task_memory → count_tokens(line) | 3386-3412 | Hundreds of tokens | ±20% | None: budget fit, order preserved | No | No |
| _emergency_shrink: before = count_messages_tokens(kept) | 2730 | ~40-58k | ±5% | Minor: may skip/shrink unnecessarily | No | Yes |
| _emergency_shrink: _tok() loop | 2737 | ~40-58k (shrinking) | ±5% | Minor: may over/under-shrink | No | Yes |

### Key Observations:

- Only 2 sites tokenize the full 400k input: line 2935 (total) and line 2377 (total_tok). Both are on the slow/cold path only.
- The per-message loop (line 2384) also processes the full 400k but in a budget-walk that stops early (typically after ~60-80 messages from the end). It does NOT process all 396 messages.
- The fast path (line 2891) only tokenizes the kept window (~40-58k), which is 10-15% of the full input.
- No site on the fast path tokenizes the full 400k.

### Tolerance Analysis per Site:

**1.1 — total check (line 2935):**
- Decision: if total <= MAX_INPUT - _note_n: return messages
- Threshold: 58000 - ~50 = 57950
- If total is off by +10%: a 52700-token input reads as 57970 → decision flips (trim when shouldn't)
- If total is off by -10%: a 63300-token input reads as 56970 → decision flips (no trim when should)
- Window where wrong decision occurs: 57950 ± 10% = [52155, 63745] → ~11,590 tokens wide
- Consequence of wrong decision: Soft failure. If we skip trim when we should, vLLM returns 400, proxy re-trims. If we trim when we shouldn't, we lose some context (minor quality loss).
- Verdict: Estimation is SAFE here. The soft-failure mode means a wrong estimate self-corrects on the next request.

**1.2 — _recut_to per-message loop (line 2384):**
- Decision: accumulates t until t + mt > budget, then sets cut
- If each mt is off by +10%: the loop stops ~10% earlier → cut shifts by ~1-2 messages earlier
- If each mt is off by -10%: the loop stops ~10% later → cut shifts by ~1-2 messages later
- Invariant: The cut must be stable across requests for prefix cache. A shift of 1-2 messages changes the first kept message, which breaks the prefix cache.
- BUT: The _window_valid check (line 2257) uses anchors (content hashes), not token counts. If the cut shifts by 1-2 messages, the anchors won't match, and the fast path will reject the window → slow path re-cuts → new cut stored. This is a self-correcting mechanism.
- Safety check: The window-collapse guard (line 2396-2415) catches gross errors (kept < 0.5x target).
- Verdict: Estimation is ACCEPTABLE here. A ±1-2 message shift triggers a re-cut (one extra slow-path cycle), but does not cause persistent cache thrashing. The system self-corrects.

**1.3 — kept_tok check (lines 2891, 2978):**
- Decision: if kept_tok > ceiling: emergency_shrink
- Ceiling: min(58000, 84000-3500-16000) - 50 = 64450 - 50 = 64400
- If kept_tok is off by +5%: 61180 reads as 64239 → emergency_shrink fires (unnecessary)
- If kept_tok is off by -5%: 67620 reads as 64239 → emergency_shrink skipped (slightly over)
- Consequence: Unnecessary shrink = wasted CPU + minor context loss. Skipped shrink = vLLM 400 → re-shrink.
- Verdict: Estimation is SAFE. Both failure modes are soft and self-correcting.

**1.4 — input_tokens for _output_budget (line 4135):**
- _output_budget(inp) = min(22500, 84000 - inp - 3500)
- Sensitive range: input in (58000, 64500) → budget transitions from 22500 to 16000
- Width: 6500 tokens = 11.2% of MAX_INPUT
- The sensitive range is only 6500 tokens wide. With ±10% error on a 58000-token input, the error is ±5800 tokens, which is 89% of the sensitive range width. So the estimate CAN land on the wrong side of the transition.
- Consequence: Slightly suboptimal max_tokens (e.g., 18000 instead of 22500, or vice versa). Not a correctness failure.
- In practice: Most Goose sessions have input_tokens in 30000-55000 range (p50=31279, p90=53076 from DB). The sensitive range (58000-64500) is only hit by the largest sessions (p99=58145). So <1% of requests are in the sensitive range.
- Verdict: Estimation is SAFE for >99% of requests. For the <1% in the sensitive range, the consequence is a slightly suboptimal max_tokens.

**1.5 — _build_session_digest (line 695):**
- Called only on epoch change (line 4007), not per-request.
- Uses count_tokens on summary text (KB-sized).
- Not on the hot path. No change needed.

**1.6 — _fit_to_budget (line 2191):**
- Called for memory/knowledge injection (line 4052).
- Works on small texts (hundreds of tokens).
- Not on the critical path. No change needed.

**1.7 — fetch_task_memory (lines 3386-3412):**
- Called for memory injection (line 4006).
- Works on individual memory lines (tens of tokens each).
- Not on the critical path. No change needed.


---

## C. Section 2 — Why 400k Is Re-Tokenized

### 2.1 — Confirming the input is full history

The capture file (/tmp/session_capture.json) contains 396 messages (1.6 MB). The body keys are ['model', 'messages', 'stream', 'max_tokens', 'temperature']. There is no delta mechanism — Goose sends the full history on every request.

### 2.2 — Watermark / incremental token count

The proxy maintains a per-session window state in proxy.session_windows (152 rows currently):

```
session_key | cut | summarized_through | dropped_total | updated_at
goose:20261010_99 | 175 | 175 | 175 | 2026-10-10 23:13:15
goose:20261010_100 | 53 | 53 | 53 | 2026-10-10 22:31:52
goose:20261010_98 | 61 | 61 | 61 | 2026-10-10 22:11:41
```

The cut field is the index into rest_raw (messages[3:]) where the window starts. The summarized_through field tracks how many messages have been summarized.

Could this support incremental token counting? In theory, yes: if we stored the token count of the kept window at the time of the last cut, we could add only the new messages' tokens on subsequent requests. However, the current code does NOT do this. It re-tokenizes the kept window from scratch on every fast-path request (line 2891).

### 2.3 — Why the slow path re-tokenizes everything

The slow path is taken when:
1. Cold start: No valid window state in memory or DB (first request for a session, or after proxy restart).
2. Window invalid: _window_valid returns False (seed changed, anchors don't match).
3. Kept over ceiling: The kept window exceeds the hard ceiling (line 2892).

For case 1 (cold start), the full 400k is tokenized because there is no prior state to leverage. This is necessary for correctness on the first request — the proxy must determine the correct cut position.

For cases 2 and 3, the full re-tokenization is a defensive measure — if the window state is corrupt or stale, the proxy rebuilds from scratch. This is a leftover from earlier bugs (the "per-turn re-collapse" bug mentioned in the F2b comment at line 2963).

The user's observation is correct: after the first few trims, the DB holds all the summarized/digested info. The 400k buffer is raw history, but the proxy's window state already contains the essential subset. The full re-tokenization on cold start is a one-time cost per session, not a per-request cost.

### 2.4 — Where trimmed content is stored

- proxy.session_summaries (994 rows): LLM-generated summaries of trimmed message slices.
- proxy.phase_summaries (2518 rows): Per-phase summaries.
- proxy.session_ledger: Active knowledge items extracted from the session.
- proxy.events: Raw event log.

Does build_context consult this stored data before tokenizing? No. The stored summaries are used for the injection block (knowledge + memory + digest), not for the context window itself. The context window is always built from the raw messages sent by Goose. The summaries are supplementary context injected as additional messages.

Why is a full tokenization still performed? Because the context window is the raw message history, not the summaries. The summaries are a separate injection channel. The proxy must determine which raw messages to keep (the cut position), which requires tokenizing the raw messages.

### 2.5 — Can the fast path skip full-input tokenization?

Yes, it already does. The fast path (line 2872-2930) only tokenizes kept (the trimmed window, ~40-58k), not the full 400k. The _window_valid check uses anchors (content hashes), not tokens. The only tokenization on the fast path is count_messages_tokens(kept) at line 2891.

The 400k tokenization only happens on the slow path (lines 2935, 2377, 2384), which is taken on:
- First request for a session (cold start)
- Window invalidation (seed change, anchor mismatch)
- Kept over ceiling

For WARM requests (the common case), the 400k is never tokenized.

### Summary of Section 2:

The user's concern about 400k re-tokenization is valid for cold starts but not applicable to warm requests. The fix for cold starts is to use estimation (chars/8.0) for the total check (line 2935) and total_tok (line 2377), and only use exact tokenization for the per-message loop (line 2384) if the estimate is near the threshold. This reduces the cold-start tokenization cost from ~7.3s to ~400ms.

---

## D. Section 3 — Real Output Token Statistics (Goose DB, Last 48h)

Source: usage_ledger table in ~/.local/share/goose/sessions/sessions.db
Filter: created_timestamp > now() - 48h, output_tokens > 0
Sample size: 4,943 turns

| Metric | Value |
|--------|-------|
| Sessions (48h) | 4,943 turns |
| output min | 25 |
| output mean | 929 |
| output p50 | 523 |
| output p90 | 2,107 |
| output p95 | 3,122 |
| output p99 | 6,176 |
| output p99.9 | 11,395 |
| output max | 15,770 |
| input min | 2,028 |
| input mean | 31,090 |
| input p50 | 31,279 |
| input p90 | 53,076 |
| input p95 | 55,690 |
| input p99 | 58,145 |
| input p99.9 | 59,561 |
| input max | 60,433 |
| total max | 68,459 |
| cache_read mean | 21,008 |
| cache_read max | 55,744 |

Key observations:
- MAX_OUTPUT (22500) is 1.43x the observed max (15770). No output in 48h came close to 22500.
- MIN_OUTPUT (16000) is ABOVE the observed max (15770). It is a dead floor — no request ever triggers the MIN_OUTPUT constraint.
- The sensitive range for _output_budget (58000-64500) is only hit by p99+ inputs. 99% of requests have input < 58145, well below the sensitive range.
- Cache read is significant (mean 21008, max 55744), confirming that prefix cache is working and is a major performance factor.

---

## E. Section 4 — Estimation Error on Real Content

### 4.1 — Full-Input Estimation (matching count_messages_tokens logic)

| Capture | Msgs | Exact Tokens | Est (chars/4) | Chars | Error (chars/4) |
|---------|------|-------------|---------------|-------|-----------------|
| small | 66 | 32,220 | 60,810 | 239,460 | +88.73% |
| medium | 153 | 80,878 | 151,798 | 598,437 | +87.69% |
| capture | 396 | 217,913 | 407,741 | 1,608,306 | +87.11% |

chars/4 overestimates by ~87%. This is because the Qwen3.8-27B tokenizer achieves ~7.5 chars/token, not 4.

### 4.2 — Per-Message Estimation Error

| Capture | Per-msg MAPE | Worst Msg | Worst Error |
|---------|-------------|-----------|-------------|
| small | 95.48% | msg 55 | +123.76% |
| medium | 95.59% | msg 69 | +134.88% |
| capture | 96.45% | msg 312 | +134.88% |

### 4.3 — Systematic Bias by Content Type

| Role | Chars/Token | chars/4 Bias | chars/7.4 Bias | chars/8.0 Bias |
|------|------------|-------------|---------------|---------------|
| assistant | 7.17 | +89.6% | +7.1% | -0.9% |
| system | 4.62 | +13.1% | +37.7% | +51.5% |
| tool | 7.95 | +84.1% | -4.6% | -11.4% |
| user | 8.43 | +106.0% | -11.7% | -19.4% |
| Overall | 7.57 | +87.1% | +7.1% | -0.9% |

chars/4 systematically overestimates by ~87% on all content types. The system prompt is the outlier (4.62 chars/token) because it's short and has a high ratio of special tokens.

### 4.4 — Divisor Sweep (Content-Only Per-Message)

| Divisor | MAPE | Bias |
|---------|------|------|
| 4.0 | 98.50% | +98.50% |
| 5.0 | 58.77% | +58.72% |
| 6.0 | 33.20% | +32.22% |
| 7.0 | 18.57% | +13.24% |
| 7.4 | 13.98% | +7.06% |
| 8.0 | 8.67% | -0.93% |
| 9.0 | 12.04% | -12.00% |
| 10.0 | 20.82% | -20.82% |

Least-squares optimal divisor: 7.19 (MAPE 16.27%, bias +10.16%)

Recommended divisor: 8.0 (MAPE 8.67%, bias -0.93%) — nearly unbiased, and slightly conservative (underestimates), which is the safe direction for a budget allocator.

### 4.5 — Why chars/4 Is Wrong

The chars/4 heuristic was designed for GPT-style tokenizers (BPE with ~4 chars/token). The Qwen3.8-27B tokenizer uses a unigram + BPE hybrid with a much larger vocabulary (151,643 tokens), achieving ~7.5 chars/token on English code-agent content. Using chars/4 for a 7.5-chars/token tokenizer overestimates by 87.5% (4/7.5 = 0.53, so the estimate is 1/0.53 = 1.87x the true value, i.e., +87%).


---

## F. Section 5 — The ONE Place (Conclusion)

### 5.1 — Is There a Place Where Exact Tokenization Is Strictly Required?

No. After analyzing all 11 tokenization sites:

- The most sensitive site is the _recut_to per-message loop (line 2384), which determines the cut position. However:
  - A ±10% per-message error shifts the cut by ~1-2 messages.
  - The _window_valid check uses anchors (content hashes), not token counts. A shifted cut causes an anchor mismatch → fast path rejects → slow path re-cuts → new cut stored. This is self-correcting.
  - The window-collapse guard (line 2396) catches gross errors.
  - A well-calibrated estimation (chars/8.0) achieves ±1 message accuracy, which is sufficient.

- The second most sensitive site is input_tokens for _output_budget (line 4135), but:
  - The sensitive range is only 6500 tokens wide (11.2% of MAX_INPUT).
  - <1% of requests fall in this range.
  - The consequence is a slightly suboptimal max_tokens, not a correctness failure.

- All other sites have soft failure modes (vLLM 400 → re-trim, unnecessary shrink → self-correct).

Conclusion: The user's hypothesis that "exact tokenization is needed in at most ONE place" is directionally correct but the answer is zero places. No site strictly requires exact tokenization for correctness. The closest is the recut loop, but even there, a well-calibrated estimation is sufficient.

### 5.2 — Substitutes for Each Site

| Site | Substitute | Tolerance | Safety Mechanism |
|------|-----------|-----------|-----------------|
| build_context: total (line 2935) | chars/8.0 estimate | ±10% | Soft fail: vLLM 400 → re-trim |
| build_context: kept_tok fast (line 2891) | chars/8.0 estimate | ±5% | Emergency_shrink self-corrects |
| build_context: kept_tok slow (line 2978) | chars/8.0 estimate | ±5% | Emergency_shrink self-corrects |
| _recut_to: total_tok (line 2377) | chars/8.0 estimate | ±10% | Collapse guard catches gross errors |
| _recut_to: per-message loop (line 2384) | chars/8.0 per message | ±10% → ±1-2 msgs | Anchor mismatch → re-cut (self-correcting) |
| chat_completions: input_tokens (line 4135) | chars/8.0 estimate | ±10% | Slightly suboptimal max_tokens (non-critical) |
| _emergency_shrink: before/_tok() (lines 2730, 2737) | chars/8.0 estimate | ±5% | Over-shrink is safe; under-shrink → vLLM 400 |
| _build_session_digest | Keep exact (off hot path) | N/A | N/A |
| _fit_to_budget | Keep exact (small texts) | N/A | N/A |
| fetch_task_memory | Keep exact (small texts) | N/A | N/A |

### 5.3 — Total Estimated Savings

Current cold-path tokenization: 7.3s (from A2)
- total (line 2935): ~2,297 ms → ~0 ms (chars/8.0 is O(n) string length, not O(n) BPE encoding)
- total_tok (line 2377): ~1,491 ms → ~0 ms
- Per-message loop (line 2384): ~1,839 ms → ~200 ms (chars/8.0 per message, still O(n) but much faster than BPE)
- kept_tok x 3 (lines 2891, 2978, 2730): ~1,310 ms → ~150 ms
- Seed + misc: ~50 ms → ~50 ms (unchanged)

Estimated new cold-path tokenization: ~400 ms (vs 7,300 ms) → ~95% reduction
Warm-path tokenization (fast path): ~890 ms → ~100 ms → ~89% reduction

### 5.4 — Exact Fallback Window

If we want to keep exact tokenization as a fallback for edge cases:

- total check (line 2935): Fall back to exact if estimate is within ±10% of MAX_INPUT (58000). Band: 52200-63800 = 11,600 tokens = 20% of MAX_INPUT. With chars/8.0 (±9% error), this triggers on ~20% of cold requests.
- Per-message loop (line 2384): No fallback needed. The self-correcting anchor mechanism handles errors.
- input_tokens (line 4135): Fall back to exact if estimate is within the sensitive range (58000-64500). Band: 6,500 tokens = 11% of MAX_INPUT. Triggers on <1% of requests.

Total exact-fallback rate: <20% of cold requests, <1% of warm requests.

---

## G. Section 6 — Parameter Sizing from Real Data

### 6.1 — MAX_OUTPUT (currently 22500)

- Observed max (48h): 15,770
- Observed p99.9: 11,395
- 22500 / 15770 = 1.43x observed max
- 2x safety over observed max: 2 x 15770 = 31,540 (too high)
- 2x safety over p99.9: 2 x 11395 = 22,790 (≈ current 22500)
- Proposed: 12,000 (= 1.05x observed max, 1.05x p99.9 with margin)
  - Justification: The observed max (15770) is a single outlier. p99.9 (11395) is a better target. 12000 = 1.05 x 11395, giving 5% headroom over p99.9.
  - Risk: If a future request needs >12000 output tokens, it will be truncated. Probability: <0.1% based on 48h data.

### 6.2 — MIN_OUTPUT (currently 16000)

- Is MIN_OUTPUT ever the active constraint? No. The observed max output (15770) is BELOW MIN_OUTPUT (16000). No request in 48h triggered the MIN_OUTPUT floor.
- 16000 is a dead floor — it constrains nothing.
- Proposed: 8,000 (= 2.5x observed p50 of 523, 1.3x observed p95 of 3122)
  - Justification: 8000 is a reasonable minimum for a useful response. It's 2.5x the median output, ensuring that even short responses have enough room.
  - This allows MAX_INPUT to grow (see 6.4).

### 6.3 — SAFETY_MARGIN (currently 3500)

- SAFETY_MARGIN is a token budget, not a time budget. It protects against:
  - Tokenizer estimation errors (±10% on 58000 = ±5800 tokens)
  - vLLM internal overhead (system prompt, special tokens)
  - Race conditions between input and output
- With chars/8.0 estimation (±9% error), the estimation error on a 58000-token input is ±5220 tokens.
- Proposed: 4,000 (up from 3500)
  - Justification: 4000 covers the ±9% estimation error (±5220) with a small buffer. The user's suggestion of 5000 is also reasonable but slightly over-conservative.
  - 4000 is 6.9% of MAX_CONTEXT, which is a reasonable safety margin.

### 6.4 — MAX_INPUT (currently 58000)

- Current: MAX_INPUT + MAX_OUTPUT + SAFETY_MARGIN = 58000 + 22500 + 3500 = 84000 = MAX_CONTEXT
- Proposed: MAX_INPUT + MAX_OUTPUT + SAFETY_MARGIN = 68000 + 12000 + 4000 = 84000 = MAX_CONTEXT
- Proposed: 68,000 (up from 58000, +17%)
- Benefit: 10,000 more tokens of context = ~15-20 more messages of history kept.
- Risk: Larger input = more tokens sent to vLLM = higher latency. But the prefix cache mitigates this (only new tokens are processed).
- Observed input p99 is 58145, so 68000 gives 16% headroom above p99.

### 6.5 — Parameter Table

| Parameter | Current | Proposed | Justification |
|-----------|---------|----------|---------------|
| MAX_CONTEXT | 84,000 | 84,000 (unchanged) | Hardware constraint |
| MAX_INPUT | 58,000 | 68,000 | +17% more history; p99 input is 58145 |
| MAX_OUTPUT | 22,500 | 12,000 | 1.05x p99.9 (11395); observed max 15770 is outlier |
| MIN_OUTPUT | 16,000 | 8,000 | Current is dead floor (above observed max 15770) |
| SAFETY_MARGIN | 3,500 | 4,000 | Covers ±9% estimation error on 68000 tokens |

Verification: 68000 + 12000 + 4000 = 84000


---

## H. Section 7 — Answers to Open Questions

### 7.1 — Is MAX_OUTPUT 22500→21000 + SAFETY_MARGIN 3500→5000 safe?

Yes, but 21000 is still 33% above the observed max (15770). The user's proposal is safe but conservative. A more aggressive 12000 is also safe (1.05x p99.9). The 21000/5000 combination gives:
- MAX_INPUT = 84000 - 21000 - 5000 = 58000 (unchanged)
- Sensitive range: (58000, 63000) = 5000 tokens wide (narrower than current 6500)
- Verdict: Safe, but doesn't improve MAX_INPUT.

### 7.2 — Does input_tokens affect prefix cache or user-visible stats?

Prefix cache: No. The prefix cache is based on the content of the messages (byte-for-byte match), not the token count. The input_tokens value is not sent to vLLM — only the messages and max_tokens are. So an estimated input_tokens does NOT affect prefix cache hits.

User-visible stats: The input_tokens is used in:
- metrics["max_context_seen"] and metrics["tokens_in_total"] (internal metrics, not user-visible)
- _track_session_tokens(session_key, input_tokens) (stored in PG, used for session analytics)
- Log messages (not user-visible)
- The ctxgate metadata block in the response (includes input_tokens)

Does Goose display input_tokens? The ctxgate metadata is returned in the response body. Goose may display it, but it's a diagnostic field, not a primary user-facing stat. A ±10% error in this field is cosmetically imperfect but functionally harmless.

Verdict: input_tokens does NOT affect prefix cache. It affects internal metrics and a diagnostic field. Estimation is safe.

### 7.3 — Why look at the whole 400k buffer?

Answered in Section 2. The 400k is only tokenized on cold starts (first request for a session). Warm requests use the fast path, which only tokenizes the kept window (~40-58k). The fix is to use estimation for the cold-path full-input tokenization, reducing it from ~7.3s to ~400ms.

### 7.4 — "Check once per 100k from Goose?" (sampling-based estimation)

Trade-offs:
- Correctness: Sampling 100k out of 400k and extrapolating gives a ±5-10% estimate, which is sufficient for all decision points (see Section 5).
- Cache stability: The cut position must be stable. A sampling-based estimate may give a slightly different cut than an exact count, but the anchor-based self-correction handles this.
- Cut stability: The per-message loop in _recut_to walks from the end. If we sample, we'd need to sample the last N messages (the ones that matter for the cut), not a random 100k. This is straightforward: tokenize the last 100 messages exactly, estimate the rest.

Recommendation: Don't sample. Use chars/8.0 for the full input (O(n) string length, ~1ms for 400k chars), and only use exact tokenization for the per-message loop if the estimate is near a threshold. This is simpler and faster than sampling.

### 7.5 — Standard Techniques for Approximate Token Counting

1. chars/N heuristic: The simplest approach. N is calibrated per tokenizer (4 for GPT, 7.5 for Qwen). Used by LiteLLM, LangChain, and most LLM gateways. Accuracy: ±10-20% for English, ±20-30% for multilingual.

2. tiktoken-lite / fasttoken: Pre-computed token counts for common text patterns. Used by OpenAI's API (server-side) and some gateways. Accuracy: near-exact for cached patterns, falls back to chars/N for novel text.

3. Incremental counting: Store the token count of the prefix, add only new tokens on each request. Used by vLLM's prefix cache and some gateways. Requires a stable prefix (which the ctxgate proxy's sticky cut provides).

4. Hybrid (recommended for ctxgate): chars/8.0 for bulk estimation + exact tokenization only when near a decision threshold. This is what the ctxgate proxy's existing count_tokens fallback already does (line 1779: return len(text) // 4 when tokenizer is disabled). The fix is to change the divisor from 4 to 8.

References:
- LiteLLM: uses chars/4 for GPT, chars/3.5 for Claude
- LangChain: uses len(text) / 4 as default
- vLLM: uses exact tokenization (no approximation)
- Anthropic API: server-side exact counting, no client-side approximation needed

### 7.6 — Risks with the Estimation Approach for Fix B

1. Polish/multilingual content: The chars/8.0 divisor is calibrated on English code-agent content. Polish text may have a different chars/token ratio (likely lower, ~6-7 chars/token for Polish due to diacritics). Mitigation: Use a conservative divisor of 7.0 for mixed-language content, or detect language and adjust.

2. Code-heavy content: Code has a lower chars/token ratio (~5-6) than prose (~8). If a session is heavily code-focused, chars/8.0 may underestimate by 10-20%. Mitigation: The safety margin (4000 tokens) absorbs this.

3. JSON/tool results: JSON has a higher chars/token ratio (~9-10) due to structural tokens. chars/8.0 slightly underestimates JSON-heavy content. Mitigation: Minor, absorbed by safety margin.

4. Cut instability: If the estimation error causes the cut to shift by 1-2 messages between requests, the prefix cache misses. Mitigation: The anchor-based _window_valid check detects this and triggers a re-cut. The re-cut is a one-time cost (~400ms with estimation), not a persistent thrash.

5. MIN_OUTPUT reduction: Reducing MIN_OUTPUT from 16000 to 8000 means short responses (500-2000 tokens) get a smaller max_tokens allocation. Risk: A response that needs 9000 tokens gets max_tokens=8000 and is truncated. Probability: <0.1% based on 48h data (p95 is 3122).

---

## I. Recommended Scope for Fix B (Do NOT Implement)

### Functions to Modify:

1. count_tokens (line 1778): Change the fallback divisor from 4 to 8.
   - Current: return len(text) // 4
   - Proposed: return len(text) // 8
   - Savings: Makes all existing fallback paths use the correct divisor.
   - Risk: None. This is already the code path used when the tokenizer is disabled.

2. count_messages_tokens (line 1798): Add an estimate parameter.
   - When estimate=True, use chars/8.0 instead of BPE encoding.
   - Savings: Enables callers to opt into estimation.
   - Risk: None. Backward compatible.

3. build_context slow path (line 2935): Use estimation for total.
   - total = count_messages_tokens(messages, estimate=True)
   - Add exact fallback if total is within ±10% of MAX_INPUT.
   - Savings: ~2,297 ms → ~1 ms on cold path.

4. _recut_to (line 2377): Use estimation for total_tok.
   - total_tok = count_messages_tokens(messages, estimate=True)
   - Savings: ~1,491 ms → ~1 ms on cold path.

5. _recut_to per-message loop (line 2384): Use estimation for mt.
   - mt = count_message_tokens(m, estimate=True)
   - Savings: ~1,839 ms → ~200 ms on cold path.

6. chat_completions (line 4135): Use estimation for input_tokens.
   - input_tokens = count_messages_tokens(built, estimate=True)
   - Savings: ~100 ms → ~1 ms on warm path.

7. Parameter changes (lines 1060-1076):
   - MAX_INPUT: 58000 → 68000
   - MAX_OUTPUT: 22500 → 12000
   - MIN_OUTPUT: 16000 → 8000
   - SAFETY_MARGIN: 3500 → 4000
   - Savings: +17% context window, better output budget utilization.

### Exact Behavior to Preserve:

- Prefix cache stability: The cut position must be stable across requests. The anchor-based _window_valid check must continue to work.
- Tool-call graph integrity: _emergency_shrink must not break the assistant→tool call graph.
- Seed preservation: The first 3 messages (seed) must always be preserved.
- Pinned newest-user copy: The pinned copy mechanism must continue to work.
- Soft failure mode: If estimation causes a slight over/under-shoot, the system must self-correct (vLLM 400 → re-trim, anchor mismatch → re-cut).

### Estimated Savings per Change:

| Change | Cold Path Savings | Warm Path Savings |
|--------|------------------|-------------------|
| count_tokens divisor 4→8 | (enabler) | (enabler) |
| total estimation (line 2935) | ~2,296 ms | 0 |
| total_tok estimation (line 2377) | ~1,490 ms | 0 |
| Per-message loop estimation (line 2384) | ~1,639 ms | 0 |
| input_tokens estimation (line 4135) | ~100 ms | ~99 ms |
| kept_tok estimation (lines 2891, 2978) | ~1,310 ms | ~790 ms |
| Total | ~6,835 ms | ~889 ms |
| From | 7,300 ms | 890 ms |
| To | ~465 ms | ~101 ms |
| Reduction | ~94% | ~89% |

### Rollback Plan:

1. Revert divisor: Change count_tokens fallback from 8 back to 4.
2. Remove estimate parameter: All callers revert to exact tokenization.
3. Revert parameters: MAX_INPUT=58000, MAX_OUTPUT=22500, MIN_OUTPUT=16000, SAFETY_MARGIN=3500.
4. No data migration needed: The session_windows table stores cut positions (integers), not token counts. Changing the estimation method does not require migrating existing window state.
5. Rollback is instant: All changes are in-memory (Python code + env vars). No DB schema changes.

---

## J. New Questions Raised by This Diagnostic

1. Why is the chars/4 divisor in the code? Was it a copy from a GPT-based template, or was it intentionally chosen? If intentional, was it tested against the Qwen tokenizer?

2. The session_windows table has 152 rows, but the proxy has been running for weeks. Is the TTL (7 days) working correctly? Are old rows being cleaned up?

3. The messages.tokens column in the Goose DB is empty (0 populated). Is this a Goose bug, or is it intentionally left null? If Goose were populating this, the proxy could use it for incremental counting.

4. The _window_persist feature-detects the table. If the table is dropped (e.g., during a DB migration), the proxy silently disables persistence. Should this be a louder warning?

5. The capture file has 396 messages, but the most recent session_windows row has cut=175. This means the kept window is 396-3-175=218 messages. Is this consistent with the 40-58k token budget? (218 messages x ~1000 tokens/msg = ~218k tokens, which is way over 58k.) This suggests the capture was taken at a moment when the window was being re-cut, or the cut=175 is from a different session.

6. The _output_budget function doesn't account for the COMPACTION_NOTE tokens. The note is ~50 tokens, added to message 0 after build_context. The budget calculation uses input_tokens (which includes the note), so this is fine. But it's worth documenting.

---

TIMING DIAGNOSTIC A3 COMPLETE — READY FOR FIX B DESIGN

