# llama-benchy → ctxgate-proxy Request Body Capture
**Date:** 2026-10-10  
**Subagent:** 20261010_90  
**Repo:** ~/Projects/llama-benchy @ e9be344578cec17745066b220798b80a0d2686d3

---

## A. Executive Summary

- **Does llama-benchy send max_tokens?** **YES.**
- **What value?** The value of `--tg` (e.g., 32). With `--exact-tg`, it also sends `"min_tokens": <tg>` and `"ignore_eos": true`.
- **Does the proxy accept it?** **NO** — the proxy returns **HTTP 413** when `max_tokens < MIN_OUTPUT (16000)`. The 413 fires before any upstream vLLM call.
- **What does that mean for MIN_OUTPUT=16000?** llama-benchy's benchmark runs (which use `--tg 32`) are **completely blocked** by the MIN_OUTPUT floor. Every benchmark request will be rejected. The floor is designed for production context-management workloads, not for benchmarking short generations.

---

## B. Step 1 — Source Code Analysis

### B.1. Repo Status

```
e9be344 add warmup run to latency mode "generation" measurement
446dd42 Added support for vLLM Rust backend with its strict request checking
7d287a3 Fixes #21
```
HEAD: `e9be344578cec17745066b220798b80a0d2686d3`

### B.2. Key Source: `_build_generation_payload` (client.py, lines 53-72)

```python
def _build_generation_payload(self, messages, max_tokens, no_cache) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": self.model_name,
        "messages": messages,
        "max_tokens": max_tokens,          # <-- ALWAYS included
        "stream": True,
        "return_token_ids": True,
        "stream_options": {"include_usage": True},
    }

    if no_cache:
        payload["cache_prompt"] = False

    payload.update(self.extra_body)

    if self.exact_tg:
        payload["max_tokens"] = max_tokens   # redundant re-set
        payload["min_tokens"] = max_tokens   # <-- forces exact length
        payload["ignore_eos"] = True         # <-- disables EOS

    return payload
```

### B.3. How `max_tokens` is Set (runner.py)

In `runner.py`, the benchmark loop passes `max_tokens=tg` to `client.run_generation()`:

```python
for tg in self.config.tg_counts:
    ...
    batch_tasks.append(self.client.run_generation(
        session,
        context_text=context,
        prompt_text=prompt,
        max_tokens=tg,          # <-- tg comes from --tg CLI arg
        no_cache=self.config.no_cache,
        ...
    ))
```

### B.4. Warmup / Latency Probe (client.py)

The warmup and latency-probe also send `max_tokens`:

```python
# Warmup (line ~250):
payload = {
    "model": self.model_name,
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 1,            # <-- 1 token
    "stream": True
}

# Coherence test (line ~230):
payload = {
    "model": self.model_name,
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 100           # <-- 100 tokens
}
```

### B.5. Definitive Answers

| Question | Answer |
|---|---|
| **Q-A:** Does llama-benchy include `max_tokens` in the HTTP POST body? | **YES** — always, in every generation request. |
| **Q-B:** What value? | The `--tg` CLI argument value (e.g., 32). With `--exact-tg`, also sends `min_tokens=<tg>` and `ignore_eos=true`. |
| **Q-C:** If NO, why not? | N/A — it always sends it. |
| **Q-DECISION:** Is `--tg` used to compute max_tokens, or only as a client-side stopping condition? | **It is sent in the request body as `max_tokens`** (and as `min_tokens` with `--exact-tg`). It is NOT merely a client-side stopping condition. The server is expected to respect it. |

---

## C. Step 3 — Live Capture (Method 3B: Proxy-side Debug Log)

tcpdump was not attempted (sudo unavailable in sandbox). Used Method 3B: added debug logging to `/tmp/sandbox_app.py` after `body = await request.json()`.

### Captured: llama-benchy Warmup Request

The warmup request was the only one captured before the 413 killed the benchmark:

```
REQ-BODY-DUMP: {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 1, "stream": true}
```

**Response:** HTTP 413
```json
{"error":{"message":"context too large for required output budget","input_tokens":85,"max_tokens":1,"min_output":16000}}
```

The benchmark aborted immediately: `Warmup failed: HTTP 413`.

> **Note:** The main benchmark requests (with `max_tokens=32`) were never sent because the warmup failed first. However, the source code analysis in Section B proves they would also be rejected (32 < 16000).

---

## D. Step 4 — Direct Curl Checks Against Sandbox

### 4.1. max_tokens=32

```
POST /v1/chat/completions
Body: {"model":"Qwen3.8-27B","messages":[{"role":"user","content":"hello"}],"max_tokens":32,"stream":false}
```

**HTTP 413**
```json
{"error":{"message":"context too large for required output budget","input_tokens":65,"max_tokens":32,"min_output":16000}}
```

### 4.2. max_tokens=17000

```
POST /v1/chat/completions
Body: {"model":"Qwen3.8-27B","messages":[{"role":"user","content":"hello"}],"max_tokens":17000,"stream":false}
```

**HTTP 200** — successful response (36 completion tokens, finish_reason=stop, no continuations)

### 4.3. max_tokens omitted

```
POST /v1/chat/completions
Body: {"model":"Qwen3.8-27B","messages":[{"role":"user","content":"hello"}],"stream":false}
```

**HTTP 200** — successful response (36 completion tokens, finish_reason=stop, no continuations)

---

## E. Decision Questions

### Q-DECISION-1: Should we drop the MIN_OUTPUT floor on the fallback path when `_client_cap` is set?

**NEEDS-HUMAN.**

The MIN_OUTPUT=16000 floor exists to guarantee enough output budget for context management (compaction, notes, etc.) in production. Dropping it when a client explicitly sets `max_tokens` would:
- Allow llama-benchy to work (good for benchmarking)
- But also allow any production client to accidentally set a tiny `max_tokens` and break context management

A safer approach: make the floor **configurable per-route** or **bypass-able via an explicit opt-in header** (e.g., `X-CTXGATE-BYPASS-MIN-OUTPUT: 1`). This keeps production safe while allowing benchmarking.

### Q-DECISION-2: If llama-benchy omits max_tokens entirely, what is the proposed alternative?

**llama-benchy does NOT omit max_tokens** — it always sends it. So this question is moot for the current version.

However, if a future version of llama-benchy (or any benchmark tool) omits `max_tokens`, the proposed alternative is:

**Treat omitted `max_tokens` as "no client cap" and apply the proxy's own MAX_OUTPUT as the effective cap.** This is already the current behavior (omitted → 200 OK). The proxy should NOT apply the MIN_OUTPUT floor when the client hasn't specified a cap, because the floor's purpose is to protect the output budget when a client *constrains* it.

---

## F. Recommended Next Steps

1. **Add a bypass mechanism for MIN_OUTPUT** — e.g., an environment variable `CTXGATE_MIN_OUTPUT=0` for benchmarking, or a request header `X-CTXGATE-BYPASS-MIN-OUTPUT`.
2. **Consider a separate "benchmark mode"** in the proxy that relaxes the output floor.
3. **Document the MIN_OUTPUT constraint** in the llama-benchy README or in ctxgate-proxy docs so users know that `--tg < 16000` will be rejected.
4. **Test with `--tg 17000`** to confirm llama-benchy works end-to-end through the proxy when the floor is satisfied.
5. **No code changes were made** to `/home/pawelw/ctxproxy/proxy/app.py`.

---

BENCHY-CAPTURE COMPLETE — NO CODE CHANGED
