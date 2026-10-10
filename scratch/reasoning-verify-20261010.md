# REASONING TOKEN VERIFICATION — MAX_OUTPUT Sizing
**Date:** 2026-10-10 23:39
**Method:** Direct vLLM probes (streaming + non-streaming), Goose DB, proxy logs

---

## A. Executive Summary

- vLLM **DOES** emit reasoning_content when preserve_thinking is false (the flag controls whether Goose stores it in history, not whether vLLM generates it).
- completion_tokens **INCLUDES** reasoning tokens. Confirmed by completion_tokens_details.reasoning_tokens field.
- **Case C applies**: vLLM emits reasoning AND completion_tokens includes reasoning.
- **MAX_OUTPUT = 16000** (observed max = 15,770; 16,000 gives 1.3% margin).
- **MAX_INPUT = 64000** (84000 - 16000 - 4000).

---

## B. Section 1 — vLLM Raw Outputs

### B.1 Streaming Request

Prompt: "What is 27 * 43? Show your work step by step with full reasoning."
max_tokens: 500, stream: true, stream_options: {include_usage: true}

Results:
- reasoning_chars (sum of delta reasoning_content): **370**
- content_chars (sum of delta content): **526**
- usage.completion_tokens: **438**
- usage.completion_tokens_details.reasoning_tokens: **170**
- Derived content_tokens: 438 - 170 = **268**

Cross-check (chars/token):
- reasoning: 370 chars / 170 tokens = 2.18 c/t (math content, very dense)
- content: 526 chars / 268 tokens = 1.96 c/t (math content, very dense)
- Total: 896 chars / 438 tokens = 2.05 c/t

Note: The low chars/token ratio is expected for mathematical content (numbers, operators, equals signs are each individual tokens in Qwen).

### B.2 Non-Streaming Request

Prompt: "What is 27 * 43?"
max_tokens: 500, stream: false

Results:
- message keys: [role, content, refusal, annotations, audio, function_call, reasoning]
- reasoning_content present: **False**
- reasoning present: **True** (key name is "reasoning", not "reasoning_content")
- reasoning len: **73** chars
- content len: **21** chars
- usage.completion_tokens: **67**
- usage.completion_tokens_details.reasoning_tokens: **50**
- Derived content_tokens: 67 - 50 = **17**

Cross-check:
- reasoning: 73 chars / 50 tokens = 1.46 c/t
- content: 21 chars / 17 tokens = 1.24 c/t

### B.3 Key Findings

1. vLLM emits reasoning in BOTH streaming and non-streaming modes when preserve_thinking is false.
2. The non-streaming key is "reasoning" (not "reasoning_content"). The streaming delta uses "reasoning_content".
3. completion_tokens = reasoning_tokens + content_tokens (confirmed: 438 = 170 + 268; 67 = 50 + 17).
4. The completion_tokens_details.reasoning_tokens field is explicitly provided by vLLM.

---

## C. Section 2 — Goose DB Schema Confirmation

### C.1 usage_ledger columns

| # | Column | Type |
|---|--------|------|
| 0 | id | INTEGER (PK) |
| 1 | session_id | TEXT |
| 2 | created_timestamp | INTEGER |
| 3 | model | TEXT |
| 4 | input_tokens | INTEGER |
| 5 | output_tokens | INTEGER |
| 6 | total_tokens | INTEGER |
| 7 | cache_read_tokens | INTEGER |
| 8 | cache_write_tokens | INTEGER |
| 9 | cost | REAL |
| 10 | cost_source | TEXT |
| 11 | is_compaction | INTEGER |

**No reasoning_tokens column exists.** The output_tokens field stores vLLM's completion_tokens which **includes reasoning**.

### C.2 Top 5 High-Output Sessions (48h)

| session_id | output_tokens | model | timestamp |
|-----------|--------------|-------|-----------|
| 20261010_102 | 7,871 | Qwen3.8-27B | 1791668162 |
| 20261010_102 | 6,724 | Qwen3.8-27B | 1791668112 |
| 20261010_102 | 13,656 | Qwen3.8-27B | 1791667469 |
| 20261010_99 | 8,758 | Qwen3.8-27B | 1791666877 |
| 20261010_99 | 6,176 | Qwen3.8-27B | 1791666309 |

All values include reasoning tokens. The overall max (from A3) is **15,770**.

---

## D. Section 3 — Proxy Log Cross-Check

**Result: 0 entries.** The proxy was restarted at 22:20 (19 minutes before this diagnostic). The journal has only 193 lines total, none containing reasoning_chars. No cross-check possible from logs.

The proxy code (line 5033) logs reasoning_chars in the NS-DIAG line, but no such line has been produced since the restart.

---

## E. Section 4 — Case Verdict

### Case C CONFIRMED

| Evidence | Value |
|----------|-------|
| vLLM emits reasoning? | **YES** (both stream and non-stream) |
| completion_tokens includes reasoning? | **YES** (438 = 170 reasoning + 268 content) |
| Observed max output_tokens (48h) | **15,770** (includes reasoning) |
| MAX_OUTPUT=12000 safe? | **NO** (15,770 > 12,000) |
| MAX_OUTPUT=16000 safe? | **YES** (15,770 < 16,000, 1.3% margin) |

**Why 12000 is unsafe:** We have observed a real request with 15,770 completion tokens (including reasoning). Setting MAX_OUTPUT=12000 would have truncated that response. The model would hit the token limit mid-generation, producing a truncated/incomplete answer.

**Why 16000 is safe:** The observed maximum is 15,770. Setting MAX_OUTPUT=16,000 gives a 230-token margin (1.3%). Given that the p99 is ~10,000 and the max is an outlier, 16,000 provides adequate headroom without wasting context budget.

---

## F. Section 5 — Goose preserves_thinking Verification

### F.1 Capture File

- Total assistant messages in /tmp/session_capture.json: **176**
- Assistant messages with reasoning key: **0**
- **Conclusion: Goose drops reasoning_content before sending history to the proxy.**

### F.2 Proxy Code Handling

| Line | Code | Purpose |
|------|------|---------|
| 2085 | strip_reasoning(messages) | Canonical: removes reasoning and reasoning_content keys from assistant messages |
| 2096-2097 | role==assistant and has reasoning keys | Filters out both key variants |
| 2104 | for m in strip_reasoning(raw) | Called in _prep_messages (all incoming messages) |
| 4790-4791 | normalize reasoning to reasoning_content | Key name normalization in streaming response |
| 5145-5151 | extract reasoning from SSE deltas | Accumulate reasoning_chars |
| 5033 | meta[ctxgate][reasoning_chars] | Log reasoning char count |

**Flow:**
1. Goose sends messages WITHOUT reasoning (preserves_thinking=false strips it client-side)
2. Proxy strip_reasoning() is a belt-and-suspenders (handles any edge case)
3. vLLM generates reasoning in the response (it always does for Qwen3.8-27B)
4. Proxy streams reasoning_content back to Goose in the SSE response
5. Goose discards it (does not store in history)
6. vLLM counts reasoning in completion_tokens, Goose stores in output_tokens

---

## G. Parameter Recommendations

| Parameter | Current | Proposed | Justification |
|-----------|---------|----------|---------------|
| MAX_CONTEXT | 84,000 | **84,000** (unchanged) | vLLM max_model_len |
| MAX_INPUT | 58,000 | **64,000** | 84000 - 16000 - 4000 |
| MAX_OUTPUT | 22,500 | **16,000** | Observed max 15,770 + 1.3% margin |
| MIN_OUTPUT | 16,000 | **8,000** | Dead floor (above observed max). 8000 = 2.5x p50 |
| SAFETY_MARGIN | 3,500 | **4,000** | Covers estimation error on larger input |

**Check:** 64,000 + 16,000 + 4,000 = 84,000 (confirmed)

---

REASONING VERIFICATION COMPLETE
