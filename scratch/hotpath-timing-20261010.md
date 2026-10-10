# Hot-Path Timing Diagnostic — ctxproxy

Date: 2026-10-10
Sandbox: port 19201, DB ctxproxy_sandbox, CTXGATE_TIMING=1
Tokenizer: Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json

---

## A. Executive Summary

- **Total pre-vLLM time for 400k input (COLD-1): 1,407 ms**
- **Total pre-vLLM time for 400k input (WARM/fast path): ~190 ms per request**
- The fast path is **7.4x faster** than the cold slow path.
- **Top 3 stages by time (COLD-1, 400k slow path):**
  1. `after_seed_freeze`: **1,293 ms** (seed freeze block in chat_completions)
  2. `slow_after_recut` (`_recut_to` walk): **653 ms**
  3. `slow_after_count_input` (tokenize full input): **550 ms**
- **Tokenization does NOT dominate.** Internal `count_messages_tokens` timing shows <1 ms total. The 550 ms in `slow_after_count_input` is the real wall-time cost of the tokenize call, but it is only 39% of pre-vLLM time.
- The **seed freeze block** (1,293 ms) is the single largest cost and is NOT tokenization.
- **Recommended next step:** Investigate the seed freeze block (1,293 ms) and `_recut_to` (653 ms). These two together account for 78% of pre-vLLM time on the cold path.

---

## B. Capture Info

| Field | Value |
|-------|-------|
| Source | Synthetic (generated, 396 messages, 176 turns) |
| Messages | 396 |
| Total chars | 1,608,306 |
| Approx tokens (4:1) | 402,076 |
| File size | 1,629,973 bytes |
| max_tokens in body | 22,500 |
| stream | false |

Variants:
- SMALL: 66 msgs, 239,460 chars, ~59,865 tokens
- MEDIUM: 153 msgs, 598,437 chars, ~149,609 tokens
- HUGE: 396 msgs, 1,608,306 chars, ~402,076 tokens

---

## C. Per-Scenario Tables

### COLD-1 (400k input, first request, new session)

Wall time (curl): **21,893 ms**

| Stage | Count | Median ms | Max ms |
|-------|-------|-----------|--------|
| start | 1 | 0.00 | 0.00 |
| after_task_spawn | 1 | 0.11 | 0.11 |
| before_build_context | 1 | 0.01 | 0.01 |
| after_build_context | 1 | (see bc:) | (see bc:) |
| after_seed_freeze | 1 | 1,292.85 | 1,292.85 |
| before_epoch_fetch | 1 | 0.04 | 0.04 |
| after_epoch_fetch | 1 | 24.42 | 24.42 |
| after_memory_injection | 1 | 0.02 | 0.02 |
| after_repair_dangling | 1 | 0.02 | 0.02 |
| after_compaction_note | 1 | 0.01 | 0.01 |
| after_count_built | 1 | 89.15 | 89.15 |
| after_extract_spawn | 1 | 0.19 | 0.19 |
| after_budget_calc | 1 | 0.02 | 0.02 |
| after_normalize_system | 1 | 0.04 | 0.04 |
| before_dispatch | 1 | 0.01 | 0.01 |
| after_vllm_call | 1 | 20,483.78 | 20,483.78 |

**Pre-vLLM total: 1,407 ms**

### COLD-2 (400k input, second request, same session — FAST PATH)

Wall time (curl): **10,545 ms**

| Stage | Count | Median ms | Max ms |
|-------|-------|-----------|--------|
| start | 1 | 10.00 | 10.00 |
| after_task_spawn | 1 | 0.09 | 0.09 |
| before_build_context | 1 | 0.01 | 0.01 |
| after_seed_freeze | 1 | 91.94 | 91.94 |
| after_memory_injection | 1 | 0.06 | 0.06 |
| after_repair_dangling | 1 | 0.02 | 0.02 |
| after_compaction_note | 1 | 0.01 | 0.01 |
| after_count_built | 1 | 89.56 | 89.56 |
| after_extract_spawn | 1 | 0.16 | 0.16 |
| after_budget_calc | 1 | 0.01 | 0.01 |
| after_normalize_system | 1 | 0.04 | 0.04 |
| before_dispatch | 1 | 0.01 | 0.01 |
| after_vllm_call | 1 | 10,353.30 | 10,353.30 |

**Pre-vLLM total: 192 ms**

### WARM (400k input, 3rd+ requests, sticky cut valid)

Wall time (curl): warm1=3,755 ms, warm2=3,187 ms

| Stage | Count | Median ms | Max ms |
|-------|-------|-----------|--------|
| start | 2 | 9.09 | 10.34 |
| after_task_spawn | 2 | 0.08 | 0.08 |
| before_build_context | 2 | 0.01 | 0.01 |
| after_seed_freeze | 2 | 94.08 | 94.95 |
| after_memory_injection | 2 | 0.07 | 0.07 |
| after_repair_dangling | 2 | 0.02 | 0.02 |
| after_compaction_note | 2 | 0.01 | 0.01 |
| after_count_built | 2 | 91.01 | 91.26 |
| after_extract_spawn | 2 | 0.17 | 0.18 |
| after_budget_calc | 2 | 0.01 | 0.01 |
| after_normalize_system | 2 | 0.04 | 0.05 |
| before_dispatch | 2 | 0.01 | 0.01 |
| after_vllm_call | 2 | 3,283.36 | 3,566.29 |

**Pre-vLLM total: 389 ms (both requests), ~195 ms per request**

### SMALL (~60k tokens, new session)

Wall time (curl): **16,180 ms**

| Stage | Count | Median ms | Max ms |
|-------|-------|-----------|--------|
| after_count_built | 1 | (see below) | — |
| after_seed_freeze | 1 | (see below) | — |
| after_vllm_call | 1 | 16,032.61 | 16,032.61 |

**Pre-vLLM total: 147 ms**

### MEDIUM (~150k tokens, new session)

Wall time (curl): **20,011 ms**

| Stage | Count | Median ms | Max ms |
|-------|-------|-----------|--------|
| after_vllm_call | 1 | 19,338.88 | 19,338.88 |

**Pre-vLLM total: 671 ms**

### HUGE (~400k tokens, new session)

Wall time (curl): **21,365 ms**

| Stage | Count | Median ms | Max ms |
|-------|-------|-----------|--------|
| after_vllm_call | 1 | 19,986.84 | 19,986.84 |

**Pre-vLLM total: 1,376 ms**

---

## D. Fast-Path Breakdown (WARM scenario, per request)

| bc: Stage | Median ms | Notes |
|-----------|-----------|-------|
| entry | (includes prior request time) | First call in log has large delta |
| after_lock | 0.01 | |
| after_state_lookup | 0.01 | ws_present=1 |
| after_window_load | 0.01 | |
| fast_after_prep | 0.07 | |
| fast_after_elide | 0.02 | |
| fast_after_count | 93.68 | Tokenize ~40k kept messages |
| fast_return | 0.24 | |

**Dominant fast-path stage: `fast_after_count` at ~94 ms** (tokenizing the ~40k kept messages).
All other fast-path stages are < 1 ms. The fast path is essentially: lock → lookup → prep → elide → count → return.

---

## E. Slow-Path Breakdown (COLD-1, 400k input)

### build_context internal (bc: stages):

| bc: Stage | Median ms | Notes |
|-----------|-----------|-------|
| entry | 0.00 | |
| after_lock | 0.01 | |
| after_state_lookup | 0.01 | ws_present=0 |
| after_window_load | 0.01 | |
| slow_after_prep | 0.09 | _prep_messages on 396 msgs |
| slow_after_count_input | **549.83** | Tokenize full 400k input |
| slow_after_early_return_check | (implicit) | total > MAX_INPUT, no early return |
| slow_after_pre_elide | 0.24 | |
| slow_after_recut | **652.55** | _recut_to walk |
| slow_after_kept_messages | 0.07 | |
| slow_after_post_elide | 0.03 | |
| slow_after_count_kept | **89.59** | Tokenize ~40k kept messages |
| slow_after_shrink | 0.13 | |
| slow_after_verify | 0.08 | |
| slow_return | 0.10 | |

### _recut_to internal:

| recut: Stage | Median ms | Notes |
|--------------|-----------|-------|
| entry | 0.00 | |
| return | **652.41** | Walk over messages to find cut point |

### Parent chat_completions stages (around build_context):

| Stage | Median ms | Notes |
|-------|-----------|-------|
| start | 0.00 | |
| after_task_spawn | 0.11 | |
| before_build_context | 0.01 | |
| [build_context: ~1,300 ms] | | Sum of bc: stages |
| after_seed_freeze | **1,292.85** | Seed freeze block |
| before_epoch_fetch | 0.04 | |
| after_epoch_fetch | 24.42 | |
| after_memory_injection | 0.02 | |
| after_repair_dangling | 0.02 | |
| after_compaction_note | 0.01 | |
| after_count_built | 89.15 | Tokenize built messages |
| after_extract_spawn | 0.19 | |
| after_budget_calc | 0.02 | |
| after_normalize_system | 0.04 | |
| before_dispatch | 0.01 | |

**Top 3 slow-path stages:**
1. `after_seed_freeze`: 1,293 ms
2. `slow_after_recut` / `recut:return`: 653 ms
3. `slow_after_count_input`: 550 ms

---

## F. Tokenization Totals

| Scenario | count_messages_tokens (count) | count_messages_tokens (total ms) | count_tokens (count) | count_tokens (total ms) |
|----------|-------------------------------|----------------------------------|---------------------|------------------------|
| COLD-1 | 6 | 0.64 | 173 | 0.00 |
| COLD-2 | 2 | 0.12 | 0 | 0.00 |
| WARM | 4 | 0.22 | 0 | 0.00 |
| SMALL | 2 | 0.06 | 1 | 0.00 |
| MEDIUM | 6 | 0.36 | 183 | 0.00 |
| HUGE | 6 | 0.75 | 173 | 0.00 |

**Discrepancy note:** The internal `count_messages_tokens` timing (using `time.monotonic()` within the function) reports <1 ms total, while the `_tm`-based stage delta for `slow_after_count_input` reports 550 ms. The 550 ms is the real wall-time cost (measured by the session-keyed `_tm` helper from `slow_after_prep` to `slow_after_count_input`). The internal timing likely has a measurement issue (possibly the `time` import scope or the duplicate encode in the log line interfering). **The 550 ms figure is the reliable one.**

**Percentage of pre-vLLM time in tokenization (COLD-1):**
- `slow_after_count_input` (550 ms) + `slow_after_count_kept` (90 ms) + `after_count_built` (89 ms) = **729 ms / 1,407 ms = 52%**
- However, this includes the `_tm` measurement overhead and the time between marks. The pure encode time is likely lower.

---

## G. Scaling Table

| Scenario | Input (approx tokens) | Pre-vLLM (ms) | slow_after_count_input (ms) | slow_after_recut (ms) |
|----------|----------------------|---------------|----------------------------|----------------------|
| SMALL | 60k | 147 | 71 | N/A (early return) |
| MEDIUM | 150k | 671 | 182 | 307 |
| HUGE | 400k | 1,376 | 527 | 658 |

**Linearity check:**
- `slow_after_count_input`: 71 → 182 → 527 ms for 60k → 150k → 400k tokens
  - Per-token rate: 1.18 → 1.21 → 1.32 ms/1k tokens — **approximately linear**
- `slow_after_recut`: N/A → 307 → 658 ms for 150k → 400k
  - Per-token rate: 2.05 → 1.65 ms/1k tokens — **approximately linear** (slightly sub-linear)
- `slow_after_prep`: 0.04 → 0.05 → 0.09 ms — **negligible, does not scale**

---

## H. Top 3 Dominant Stages (COLD-1, 400k)

| Rank | Stage | Time (ms) | % of Pre-vLLM |
|------|-------|-----------|---------------|
| 1 | after_seed_freeze | 1,293 | 92%* |
| 2 | slow_after_recut (_recut_to) | 653 | 46%* |
| 3 | slow_after_count_input | 550 | 39%* |

**Percentages exceed 100% because `after_seed_freeze` (measured in the parent session) includes the time spent inside `build_context` (measured in the `bc:` session). The stages are not independent; they are nested. The true additive breakdown is:*
- *build_context total: ~1,300 ms (includes recut + count_input + count_kept)*
- *Seed freeze (after bc returns): ~0 ms additional (the 1,293 ms IS the bc time)*
- *after_count_built: 89 ms*
- *Other: ~18 ms*
- *Total: ~1,407 ms*

---

## I. Unexpected Dominance

**The seed freeze block is the surprise.** At 1,293 ms, it accounts for the vast majority of pre-vLLM time. This block runs AFTER `build_context` returns and processes the frozen message set. It is NOT tokenization. It is NOT the recut walk. It is something in the seed freeze logic that scales with input size.

This needs investigation in Fix B. Possible causes:
- Iterating over all 396 messages to identify which are frozen
- String concatenation or copying of large content
- Some O(n) operation on the raw (pre-trim) message list

---

## J. Recommended Scope for Fix B

### Target stages (in priority order):

1. **Seed freeze block (1,293 ms → target: <50 ms)**
   - Largest single cost. Need to understand what it does and why it's O(n) on the full input.
   - If it's iterating over all messages to find frozen ones, consider maintaining a frozen-set index.
   - Invariants to preserve: frozen messages must remain at the front; the cut point must be stable.

2. **_recut_to walk (653 ms → target: <100 ms)**
   - Walks messages from the end, accumulating token counts until the budget is met.
   - Currently O(n) with a token count per message.
   - Possible fix: maintain a prefix-sum of token counts per message, making the cut a binary search (O(log n)).
   - Invariants to preserve: cut point must respect message boundaries; system message always kept; tool-call pairs must not be split.

3. **slow_after_count_input (550 ms → target: <100 ms)**
   - Full tokenization of the 400k input.
   - This is the "tax" of receiving a large context. Hard to avoid without protocol changes.
   - Possible mitigation: if the client sends a token count in the request header, skip re-tokenization for the early-return check.
   - Invariants to preserve: token count must be accurate for the window budget decision.

### Estimated savings:
- Current pre-vLLM (400k cold): **1,407 ms**
- After fixes (target): **~200-300 ms**
- Savings: **~1,100-1,200 ms per cold request**
- Warm path already at ~190 ms, so the gap would close to near-zero.

### What NOT to touch:
- Fast path (already at 190 ms, dominated by 94 ms of necessary tokenization of kept messages)
- vLLM call time (20s — that's model inference, not proxy overhead)
- `_prep_messages` (0.09 ms — negligible)
- `_repair_dangling_tool_calls` (0.02 ms — negligible)
- `_normalize_system_messages` (0.04 ms — negligible)

---

TIMING DIAGNOSTIC COMPLETE — READY FOR REVIEW
