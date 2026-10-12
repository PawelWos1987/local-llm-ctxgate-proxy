"""LM (Mistral) API call execution and queue submission."""
import asyncio
import json
import logging
import time

import httpx

from proxy.config.constants import MISTRAL_MODEL, MISTRAL_URL, LM_PRI_HIGH, LM_PRI_LOW
from proxy.resilience.breaker import _check_lm_breaker, _lm_breaker
from proxy.token.counter import count_tokens
from proxy.lm.rate_limiter import _lm_rate_limiter
import re

log = logging.getLogger("ctxgate-proxy")

_lm_seq_counter = 0

# TODO(I4): replace with AppContext


async def _lm_do_call(task: dict):
    """Execute a single Mistral API call with rate limiting."""
    # TODO(I4): replace with AppContext
    from proxy.app import _lm_client, metrics
    global _lm_client
    messages = list(task["messages"])
    if task.get("system"):
        messages.insert(0, {"role": "system", "content": task["system"]})

    prompt_text = " ".join(
        m.get("content", "") if isinstance(m.get("content", ""), str) else str(m.get("content", ""))
        for m in messages
    )
    estimated_tokens = count_tokens(prompt_text) + task["max_tokens"]

    _check_lm_breaker()  # raises if open
    await _lm_rate_limiter.acquire(estimated_tokens)

    t0 = time.monotonic()
    body = {
        "model": MISTRAL_MODEL,
        "messages": messages,
        "max_tokens": task["max_tokens"],
        "temperature": task["temperature"],
    }
    try:
        resp = await _lm_client.post(MISTRAL_URL + "/chat/completions", json=body)
        _lm_breaker.record_success()
    except (httpx.ConnectError, httpx.TransportError) as e:
        _lm_breaker.record_failure()
        _lm_rate_limiter.release()
        raise

    data = None
    actual_tokens = 0
    if resp.status_code == 200:
        try:
            data = resp.json()
            actual_tokens = data.get("usage", {}).get("total_tokens", 0)
            # Accumulate token usage per call kind
            _kind = task.get("kind", "other")
            if _kind not in metrics["lm_tokens_by_kind"]:
                metrics["lm_tokens_by_kind"][_kind] = {"prompt": 0, "completion": 0}
                metrics["lm_calls_by_kind"][_kind] = 0
            metrics["lm_calls_by_kind"][_kind] += 1
            _usage = data.get("usage", {})
            metrics["lm_tokens_by_kind"][_kind]["prompt"] += _usage.get("prompt_tokens", 0)
            metrics["lm_tokens_by_kind"][_kind]["completion"] += _usage.get("completion_tokens", 0)
        except Exception:
            pass
    _lm_rate_limiter.release(actual_tokens, estimated_tokens)

    dt = time.monotonic() - t0
    if resp.status_code == 429:
        log.warning("Mistral rate limited (%.1fs): %s", dt, resp.text[:200])
        return {} if task["json_mode"] else ""
    if resp.status_code != 200:
        log.warning("Mistral API error %d (%.1fs): %s", resp.status_code, dt, resp.text[:200])
        return {} if task["json_mode"] else ""

    msg = data.get("choices", [{}])[0].get("message", {})
    content = msg.get("content", "")
    if not content:
        return {} if task["json_mode"] else ""
    if not task["json_mode"]:
        return content.strip()
    content = content.strip()
    if content.startswith("```"):
        segs = content.split("\n")
        content = "\n".join(segs[1:])
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return json.loads(content)


async def _call_4b(messages, max_tokens=2000, json_mode=True, priority=LM_PRI_HIGH, system=None, kind="other"):
    """Submit a Mistral API call to the priority queue. Awaits the result.

    Priority: LM_PRI_HIGH (0) for trim summaries, LM_PRI_LOW (2) for knowledge.
    Uses a shared httpx client and 2 consumer tasks, so Mistral API
    is never hit with more than 2 concurrent requests.
    """
    # TODO(I4): replace with AppContext
    from proxy.app import _lm_queue, _lm_client
    if _lm_queue is None or _lm_client is None:
        log.warning("Mistral queue not ready - _call_4b skipped")
        return {}
    _lm_seq_counter += 1
    future = asyncio.get_event_loop().create_future()
    task = {
        "priority": priority,
        "seq": _lm_seq_counter,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "json_mode": json_mode,
        "system": system,
        "kind": kind,
        "future": future,
        "enqueued_at": time.time(),
    }
    await _lm_queue.put((priority, _lm_seq_counter, task))
    try:
        return await asyncio.wait_for(future, timeout=300)
    except asyncio.TimeoutError:
        log.warning("Mistral queue call timed out after 300s (pri=%d)", priority)
        return {}

async def _call_lm_4b(prompt: str, system: str = "Return only valid JSON. No markdown, no commentary.", temperature: float = 0.3, max_tokens: int = 512, priority: int = LM_PRI_LOW, kind: str = "other") -> str:
    """Submit a Mistral API call to the priority queue. Returns raw text or empty string on failure."""
    # TODO(I4): replace with AppContext
    from proxy.app import _lm_queue
    if _lm_queue is None:
        log.debug("Mistral queue not ready, _call_lm_4b skipped")
        return ""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    result = await _call_4b(messages, max_tokens=max_tokens, json_mode=False, priority=priority, kind=kind)
    if isinstance(result, str):
        return result
    return ""


_EXTRACT_SEM = asyncio.Semaphore(2)

