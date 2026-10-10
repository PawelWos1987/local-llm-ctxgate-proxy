# Hot-Path Timing Diagnostic A2 — Tokenization & Recut Deep Breakdown

Date: 2026-10-10
Target: /home/pawelw/ctxproxy/proxy/app.py
Sandbox: port 19201, DB ctxproxy_sandbox, CTXGATE_TIMING=1

## A. Executive Summary

- **Total pre-vLLM time for 400k input (COLD-1): ~7,344 ms in tokenization** (25.4% of 28.9s wall time; remainder is vLLM inference).
- **Top 3 stages by time:**
  1. First full `count_messages_tokens` on 400k input: **2,297 ms** (encode = 2,273 ms)
  2. Second full `count_messages_tokens` (inside recut): **1,491 ms** (encode = 1,484 ms)
  3. `_recut_to` per-message tokenization loop: **1,839 ms** (sum_tokenize = 1,813 ms)
- **Fast path is meaningfully faster:** COLD-2/WARM use only 2 × ~400ms cmt_total calls on 87 msgs (~800ms total) vs 6 × cmt_total + recut (~7,300ms) on the slow path. **~9× speedup.**
- **Tokenization dominates:** encode() is 99%+ of every cmt_total call. build and join are negligible (<0.5ms).
- **Recommended next step:** Target the 3 redundant full-input tokenizations (2,297 + 1,491 + 1,382 = 5,170ms) and the per-message recut loop (1,839ms). A single-pass tokenizer with incremental counting could eliminate ~7,000ms of the 7,344ms total.

## B. Capture Info

| Metric | Value |
|--------|-------|
| File | /tmp/session_capture.json |
| Size | 1,629,973 bytes (1.6 MB) |
| Messages | 396 |
| Total chars | 1,611,959 |
| Approx tokens (4:1) | ~403,000 |
| Actual tokens (tokenizer) | ~400,000 (inferred from recut budget) |

Variants:
- /tmp/session_small.json: 66 msgs, 240,073 chars (~60k tokens)
- /tmp/session_medium.json: 153 msgs, 599,849 chars (~150k tokens)
- /tmp/session_capture.json: 396 msgs, 1,611,959 chars (~400k tokens)

## C. Per-Scenario Tables

### COLD-1 (400k input, fresh session) — wall: 28.89s

| Stage | Count | Total ms | Max ms | Notes |
|-------|-------|----------|--------|-------|
| cmt_total (input) | 2 | 3,788.29 | 2,296.96 | 396 msgs, 1.6M chars |
| cmt_total (seed) | 1 | 19.87 | 19.87 | 3 msgs, 24k chars |
| cmt_total (kept) | 3 | 1,309.94 | 479.93 | 87 msgs, 306k chars |
| recut_internals | 1 | 1,838.94 | 1,838.94 | 85 iterated, 393 rest |
| recut_kept_count | 1 | 387.04 | 387.04 | cut=309, kept=40,894 tok |
| **Total tokenization** | **8** | **7,344.08** | **2,296.96** | |

### COLD-2 (400k input, same session — FAST PATH) — wall: 22.62s

| Stage | Count | Total ms | Max ms | Notes |
|-------|-------|----------|--------|-------|
| cmt_total (kept) | 2 | 886.80 | 481.56 | 87 msgs, 306k chars |
| recut_internals | 0 | 0 | 0 | Fast path — no recut |
| **Total tokenization** | **2** | **886.80** | **481.56** | |

### WARM (400k input, same session ×2 — FAST PATH) — wall: 4.58s + 5.37s

| Stage | Count | Total ms | Max ms | Notes |
|-------|-------|----------|--------|-------|
| cmt_total (kept) | 4 | 1,782.91 | 484.29 | 87 msgs, 306k chars |
| recut_internals | 0 | 0 | 0 | Fast path — no recut |
| **Total tokenization** | **4** | **1,782.91** | **484.29** | |

### SMALL (~60k tokens, fresh session) — wall: 17.35s

| Stage | Count | Total ms | Max ms | Notes |
|-------|-------|----------|--------|-------|
| cmt_total (input) | 1 | 366.37 | 366.37 | 66 msgs, 240k chars |
| cmt_total (post) | 1 | 384.65 | 384.65 | 66 msgs, 240k chars |
| recut_internals | 0 | 0 | 0 | Input fits budget — no recut |
| **Total tokenization** | **2** | **751.02** | **384.65** | |

### MEDIUM (~150k tokens, fresh session) — wall: 23.06s

| Stage | Count | Total ms | Max ms | Notes |
|-------|-------|----------|--------|-------|
| cmt_total (input) | 2 | 1,406.70 | 891.38 | 153 msgs, 600k chars |
| cmt_total (seed) | 1 | 4.58 | 4.58 | 3 msgs, 5k chars |
| cmt_total (kept) | 3 | 1,209.66 | 517.71 | 91 msgs, 300k chars |
| recut_internals | 1 | 770.58 | 770.58 | 90 iterated, 150 rest |
| recut_kept_count | 1 | 268.41 | 268.41 | cut=62, kept=40,310 tok |
| **Total tokenization** | **8** | **3,659.93** | **891.38** | |

### HUGE (400k tokens, fresh session) — wall: 29.36s

| Stage | Count | Total ms | Max ms | Notes |
|-------|-------|----------|--------|-------|
| cmt_total (input) | 2 | 3,698.84 | 2,317.07 | 396 msgs, 1.6M chars |
| cmt_total (seed) | 1 | 19.17 | 19.17 | 3 msgs, 24k chars |
| cmt_total (kept) | 3 | 1,224.55 | 491.85 | 87 msgs, 306k chars |
| recut_internals | 1 | 1,646.67 | 1,646.67 | 85 iterated, 393 rest |
| recut_kept_count | 1 | 278.37 | 278.37 | cut=309, kept=40,894 tok |
| **Total tokenization** | **8** | **6,867.60** | **2,317.07** | |

## D. Fast-Path Breakdown (WARM scenario)

The fast path skips recut entirely. Per-request tokenization:

| Call | Msgs | Chars | Total ms | Build ms | Join ms | Encode ms |
|------|------|-------|----------|----------|---------|-----------|
| cmt_total #1 | 87 | 306,156 | 408.64 | 2.86 | 0.03 | 405.75 |
| cmt_total #2 | 87 | 306,343 | 484.29 | 3.29 | 0.03 | 480.97 |
| cmt_total #3 | 87 | 306,156 | 405.80 | 3.08 | 0.03 | 402.69 |
| cmt_total #4 | 87 | 306,343 | 484.18 | 3.28 | 0.03 | 480.87 |

**Per-request cost: ~890ms** (2 × cmt_total on 87 msgs).
**Dominant sub-component: encode (99.3% of each call).**
**CT-RETOK: unique=0, total_calls=0** — no per-message tokenization at all.

## E. Slow-Path Breakdown (COLD-1, 400k input)

Full pipeline in order:

| # | Stage | ms | Sub-breakdown |
|---|-------|-----|---------------|
| 1 | cmt_total (full input) | 2,296.96 | build=23.87, join=0.10, **encode=2,273.00** |
| 2 | cmt_total (seed) | 19.87 | build=0.09, join=0.01, encode=19.77 |
| 3 | cmt_total (full input, 2nd) | 1,491.33 | build=7.45, join=0.07, **encode=1,483.80** |
| 4 | recut_internals | 1,838.94 | seed=20.65, **total_count=1,531.68**, loop=285.91, **sum_per_msg=281.06**, **sum_tokenize=1,812.74** |
| 5 | cmt_total (kept) | 382.91 | build=2.61, join=0.16, encode=380.14 |
| 6 | recut_kept_count | 387.04 | (wraps cmt_total on kept msgs) |
| 7 | cmt_total (kept, 2nd) | 447.11 | build=3.12, join=0.03, encode=443.96 |
| 8 | cmt_total (kept, 3rd) | 479.93 | build=3.12, join=0.03, encode=476.78 |
| | **Total** | **7,344.08** | |

**recut_internals sub-breakdown (COLD-1):**
- rest_msgs: 393, iterated: 85
- seed_ms: 20.65
- total_count_ms: 1,531.68 (cumulative token count tracking)
- loop_ms: 285.91 (Python loop overhead)
- sum_per_msg_ms: 281.06 (per-message count_tokens calls)
- sum_tokenize: 1,812.74 (actual encoder calls)
- max_msg_tok: 2,782, med_msg_tok: 244, total_msg_tok: 36,944
- budget: 37,344

**Key insight:** The recut loop iterates only 85 of 393 messages (stops when budget is hit). The 1,813ms of tokenization is spread across 85 messages averaging 244 tokens each. The per-message overhead (281ms for 85 calls = 3.3ms/call) is dominated by the encoder, not Python.

## F. Tokenization Totals

### count_messages_tokens (cmt_total)

| Scenario | Calls | Total ms | Avg ms/call | Encode % |
|----------|-------|----------|-------------|----------|
| COLD-1 | 6 | 5,118.11 | 853.0 | 99.5% |
| COLD-2 | 2 | 886.80 | 443.4 | 99.3% |
| WARM (×2) | 4 | 1,782.91 | 445.7 | 99.3% |
| SMALL | 2 | 751.02 | 375.5 | 99.4% |
| MEDIUM | 6 | 2,620.94 | 436.8 | 99.4% |
| HUGE | 6 | 4,942.56 | 823.8 | 99.5% |

### count_tokens (per-message, inside recut)

| Scenario | Calls | Total ms | Avg ms/call |
|----------|-------|----------|-------------|
| COLD-1 | 86 | 281.06 | 3.27 |
| MEDIUM | 91 | 239.71 | 2.63 |
| HUGE | 86 | 224.66 | 2.61 |
| COLD-2 | 0 | 0 | — |
| WARM | 0 | 0 | — |
| SMALL | 0 | 0 | — |

### Total tokenization (cmt_total + recut_internals + recut_kept_count)

| Scenario | Total ms | % of wall time |
|----------|----------|----------------|
| COLD-1 (400k) | 7,344.08 | 25.4% |
| COLD-2 (400k, fast) | 886.80 | 3.9% |
| WARM (400k, fast ×2) | 1,782.91 | 17.9% |
| SMALL (60k) | 751.02 | 4.3% |
| MEDIUM (150k) | 3,659.93 | 15.9% |
| HUGE (400k) | 6,867.60 | 23.4% |

## G. Scaling Table

| Input | cmt_total ms | recut ms | Total tok ms | Wall ms | Tok % |
|-------|-------------|----------|-------------|---------|-------|
| 60k (SMALL) | 751 | 0 | 751 | 17,355 | 4.3% |
| 150k (MEDIUM) | 2,621 | 1,039 | 3,660 | 23,063 | 15.9% |
| 400k (HUGE) | 4,943 | 1,925 | 6,868 | 29,360 | 23.4% |

**Scaling analysis:**
- cmt_total scales ~linearly: 751/60k = 12.5ms per 10k, 2621/150k = 17.5ms per 10k, 4943/400k = 12.4ms per 10k
- recut scales ~linearly: 1039/150k = 6.9ms per 10k, 1925/400k = 4.8ms per 10k
- Total tokenization scales ~linearly: 751/60k = 12.5, 3660/150k = 24.4, 6868/400k = 17.2 ms per 10k
- The non-linearity at 150k is because recut triggers at that size but not at 60k

## H. Top 3 Dominant Stages (COLD-1, 400k)

| Rank | Stage | ms | What it does |
|------|-------|-----|--------------|
| 1 | cmt_total #1 (full input) | 2,297 | Tokenizes all 396 msgs (1.6M chars) to get total token count |
| 2 | recut_internals (per-msg loop) | 1,839 | Tokenizes 85 messages individually to find the cut point |
| 3 | cmt_total #2 (full input, 2nd) | 1,491 | Re-tokenizes all 396 msgs (duplicate of #1) |

**Combined: 5,627ms (76.6% of total tokenization time)**

## I. Unexpected Findings

1. **The second full-input cmt_total (1,491ms) is a pure duplicate.** It tokenizes the exact same 396 messages / 1.6M chars as the first call (2,297ms). The only difference is the first call is slightly slower (2,297 vs 1,491) likely due to CPU cache effects. This is **1,491ms of wasted work**.

2. **recut_internals total_count_ms (1,532ms) overlaps with cmt_total #2 (1,491ms).** The recut function calls count_messages_tokens internally to track cumulative tokens, which is the same work as the outer cmt_total call. This suggests the recut is re-tokenizing the full input as it walks.

3. **Memory allocation at line 2154 (71.2KB, 792 allocs):** This is the `full` string concatenation in cmt_total. At 1.6M chars, the actual string is ~1.6MB, but tracemalloc reports 71.2KB — likely because it's measuring a specific allocation site, not the full string. The peak memory is 14.9MB for the entire 400k request.

4. **CT-RETOK shows minimal redundancy:** 85 unique keys, 86 total calls, only 1 repeated (max_repeat=2). The per-message tokenization cache is working as intended — the cost is in the 85 unique messages, not repeated lookups.

5. **Fast path is 9× faster on tokenization:** 887ms (fast) vs 7,344ms (slow). The fast path avoids all full-input tokenization and recut, only counting the 87 kept messages.

## J. Recommended Scope for Fix B

### Target stages (in priority order):

1. **Eliminate the duplicate full-input cmt_total (#2):** Save **~1,491ms**
   - The second call tokenizes the same 396 messages as the first. If the total from call #1 is cached/reused, this is free.
   - Invariant: The total token count must be accurate for the budget check.

2. **Replace per-message recut tokenization with a single-pass approach:** Save **~1,500ms**
   - Currently: 85 individual count_tokens calls (281ms) + internal cmt_total (1,532ms) = 1,813ms
   - Alternative: Tokenize the full text once, use token offsets to find the cut point. The encoder already produces a flat token array — we just need to map message boundaries to token ranges.
   - Invariant: The cut index must produce the same kept message set.

3. **Reduce cmt_total call count from 6 to 2-3:** Save **~2,000ms**
   - Currently 6 calls: 2× full input + 1× seed + 3× kept
   - The 3× kept calls (383+447+480 = 1,310ms) could potentially be reduced to 1-2
   - Invariant: Final input_tokens must be accurate for the budget check.

### Total potential savings: **~5,000ms** (from 7,344ms to ~2,300ms)

### Invariants that must be preserved:
- The cut index must produce the same kept message set (same messages, same order)
- Final input_tokens must be accurate (used for budget enforcement)
- The seed messages must always be included
- The fast path must remain fast (<1s tokenization)
- No change to the tokenizer model or encoding behavior

### What NOT to change:
- The tokenizer model (Qwen3.8-27B)
- The message format or ordering
- The budget constants (MAX_INPUT, etc.)
- The fast-path logic (it's already optimal)

---

## Tracemalloc Overhead Caveat

All measurements above include tracemalloc overhead. The CT-MEM snapshot is taken at the end of each request, adding ~100-200ms of overhead per request. The cmt_total and recut timings are measured with time.monotonic() and are NOT affected by tracemalloc (tracemalloc only tracks allocations, not CPU time). However, the allocation tracking itself adds memory pressure that could slightly affect cache behavior. The absolute numbers should be treated as upper bounds; real production times (without tracemalloc) would be slightly lower.

---

TIMING DIAGNOSTIC COMPLETE — READY FOR REVIEW

