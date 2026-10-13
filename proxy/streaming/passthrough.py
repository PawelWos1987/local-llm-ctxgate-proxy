"""Passthrough streaming/forwarding: no context management, direct proxy."""
import logging

from proxy.streaming.sse import _sse_content, _lines_with_heartbeat, _safe_truncate
import asyncio
import json
import httpx
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


async def forward_to_vllm_passthrough(vllm_body: dict,
                                      input_tokens: int,
                                      session_key: str):
    """Raw JSON passthrough for client-capped (fallback) requests.

    One POST to vLLM. No loop detection, no retries, no
    continuation, no ctxgate metadata. Forwards vLLM's JSON
    response verbatim (only injects prompt_tokens for
    consistency). Tracks usage for metrics.
    """
    # Pre-send token guard: protect against estimator underestimation
    from proxy.window.shrink import _emergency_shrink
    from proxy.config.constants import MAX_CONTEXT, SAFETY_MARGIN, MAX_OUTPUT
    from proxy.token.counter import count_messages_tokens

    _ceiling = MAX_CONTEXT - SAFETY_MARGIN - MAX_OUTPUT
    _estimated = count_messages_tokens(vllm_body.get("messages", []))
    if _estimated > _ceiling:
        log.warning("Passthrough pre-send guard (json): input=%d > ceiling=%d - shrinking", _estimated, _ceiling)
        vllm_body["messages"] = _emergency_shrink(vllm_body["messages"], _ceiling)
        _estimated = count_messages_tokens(vllm_body["messages"])
        if _estimated > _ceiling:
            return JSONResponse(
                {"error": {"message": "context exceeds maximum after shrink",
                          "input_tokens": _estimated, "max_context": MAX_CONTEXT}},
                status_code=413)

    import sys
    _app = sys.modules["proxy.app"]
    from proxy.config.constants import VLLM_URL, VLLM_MODEL
    try:
        client = _app._vllm_client
        _app._check_vllm_breaker()
        body = {k: v for k, v in vllm_body.items()
                if not k.startswith("_ctxgate_")}
        _serialized = json.dumps(body); log.warning("OUTBOUND_BODY: session=%s bytes=%d chars=%d", session_key, len(_serialized.encode('utf-8')), len(_serialized))
        resp = await client.post(
            VLLM_URL + "/chat/completions", json=body)
        if resp.status_code != 200:
            _app._vllm_breaker.record_failure()
            _app.metrics["requests_error"] += 1
            _app._record_call(session_key, input_tokens, 0,
                         f"vllm_{resp.status_code}",
                         VLLM_MODEL, False, resp.text[:300])
            return JSONResponse(
                {"error": {"message":
                 "vLLM " + str(resp.status_code),
                 "explanation": _app.explain_status(
                     f"vllm_{resp.status_code}",
                     resp.text[:300])}},
                status_code=resp.status_code)
        _app._vllm_breaker.record_success()
        data = resp.json()
        if not isinstance(data.get("usage"), dict):
            data["usage"] = {}
        out_tok = data["usage"].get("completion_tokens", 0)
        cached = (data["usage"].get(
            "prompt_tokens_details") or {}).get("cached_tokens", 0)
        _real_pt = data["usage"].get("prompt_tokens", 0)
        _final_pt = _real_pt if _real_pt > 0 else input_tokens
        data["usage"]["prompt_tokens"] = _final_pt
        data["usage"]["total_tokens"] = _final_pt + out_tok
        # Invariant: cached_tokens must never exceed prompt_tokens
        _ptd = data["usage"].get("prompt_tokens_details")
        if _ptd and _ptd.get("cached_tokens", 0) > _final_pt:
            _ptd["cached_tokens"] = _final_pt
        from proxy.hotpath.tracking import store_exact_tokens
        store_exact_tokens(session_key, _final_pt)
        _app.metrics["requests_ok"] += 1
        _app.metrics["tokens_out_total"] += out_tok
        _app.metrics["cached_tokens_total"] += cached
        _app.metrics["prompt_tokens_total"] += _final_pt
        _app._track_session_tokens(session_key, 0, out_tok,
                              count_req=False)
        _app._record_call(session_key, _final_pt, out_tok, "ok",
                     VLLM_MODEL, False, cached_tokens=cached)
        log.info("PASSTHROUGH-JSON session=%s in=%d out=%d",
                 session_key, input_tokens, out_tok)
        return JSONResponse(data)
    except httpx.TimeoutException:
        _app.metrics["requests_error"] += 1
        _app._vllm_breaker.record_failure()
        _app._record_call(session_key, input_tokens, 0, "timeout",
                     VLLM_MODEL, False, "vLLM timeout")
        return JSONResponse(
            {"error": {"message": "vLLM timeout",
             "explanation": _app.explain_status("timeout")}},
            status_code=504)
    except Exception as e:
        _app.metrics["requests_error"] += 1
        _app._vllm_breaker.record_failure()
        log.exception("PASSTHROUGH-JSON session=%s error: %s",
                      session_key, e)
        _app._record_call(session_key, input_tokens, 0, "error",
                     VLLM_MODEL, False, str(e)[:300])
        return JSONResponse(
            {"error": {"message": str(e)[:200],
             "explanation": _app.explain_status("error", str(e)[:300])}},
            status_code=500)


async def stream_to_vllm_passthrough(vllm_body: dict,
                                     input_tokens: int,
                                     session_key: str):
    """Raw SSE passthrough for client-capped (fallback) requests.

    One POST to vLLM. No loop detection, no retries, no
    continuation, no seam dedup, no ctxgate metadata. Forwards
    vLLM's SSE stream line-for-line so token_ids and any
    vendor-specific fields survive. Tracks usage for metrics.
    """
    # Pre-send token guard: protect against estimator underestimation
    from proxy.window.shrink import _emergency_shrink
    from proxy.config.constants import MAX_CONTEXT, SAFETY_MARGIN, MAX_OUTPUT
    from proxy.token.counter import count_messages_tokens

    _ceiling = MAX_CONTEXT - SAFETY_MARGIN - MAX_OUTPUT
    _estimated = count_messages_tokens(vllm_body.get("messages", []))
    if _estimated > _ceiling:
        log.warning("Passthrough pre-send guard (stream): input=%d > ceiling=%d - shrinking", _estimated, _ceiling)
        vllm_body["messages"] = _emergency_shrink(vllm_body["messages"], _ceiling)
        _estimated = count_messages_tokens(vllm_body["messages"])
        if _estimated > _ceiling:
            return JSONResponse(
                {"error": {"message": "context exceeds maximum after shrink",
                          "input_tokens": _estimated, "max_context": MAX_CONTEXT}},
                status_code=413)

    async def generate():
        import time as _t
        import sys
        _app = sys.modules["proxy.app"]
        from proxy.config.constants import VLLM_URL, VLLM_MODEL
        t0 = _t.monotonic()
        total_out = 0
        total_cached = 0
        _actual_pt = 0
        try:
            client = _app._vllm_client
            _app._check_vllm_breaker()
            body = {k: v for k, v in vllm_body.items()
                    if not k.startswith("_ctxgate_")}
            async with client.stream(
                    "POST", VLLM_URL + "/chat/completions",
                    json=body) as resp:
                if resp.status_code != 200:
                    body_bytes = await resp.aread()
                    _app._vllm_breaker.record_failure()
                    _app.metrics["requests_error"] += 1
                    _app._record_call(session_key, input_tokens, 0,
                                 f"vllm_{resp.status_code}",
                                 VLLM_MODEL, True,
                                 body_bytes[:300].decode(
                                     "utf-8", errors="replace"))
                    yield "data: " + json.dumps({
                        "error": body_bytes[:200].decode(
                            "utf-8", errors="replace")
                    }) + "\n\n"
                    yield "data: [DONE]\n\n"
                    return
                _app._vllm_breaker.record_success()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        if line.strip():
                            yield line + "\n\n"
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        yield "data: [DONE]\n\n"
                        break
                    try:
                        chunk = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        yield line + "\n\n"
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        total_out = usage.get(
                            "completion_tokens", total_out)
                        total_cached += (usage.get(
                            "prompt_tokens_details") or {}).get(
                            "cached_tokens", 0)
                        _pt = usage.get("prompt_tokens", 0)
                        if _pt > 0:
                            _actual_pt = _pt
                    yield line + "\n\n"
            wall = _t.monotonic() - t0
            _final_pt = _actual_pt if _actual_pt > 0 else input_tokens
            from proxy.hotpath.tracking import store_exact_tokens
            store_exact_tokens(session_key, _final_pt)
            _app.metrics["requests_ok"] += 1
            _app.metrics["tokens_out_total"] += total_out
            _app.metrics["cached_tokens_total"] += total_cached
            _app.metrics["prompt_tokens_total"] += _final_pt
            _app._track_session_tokens(session_key, 0, total_out,
                                  count_req=False)
            _app._record_call(session_key, _final_pt, total_out,
                         "ok", VLLM_MODEL, True,
                         cached_tokens=total_cached)
            log.info(
                "PASSTHROUGH-STREAM session=%s in=%d out=%d "
                "wall=%.2fs", session_key, input_tokens,
                total_out, wall)
        except asyncio.CancelledError:
            log.info("PASSTHROUGH-STREAM session=%s client "
                     "disconnected", session_key)
            raise
        except Exception as e:
            _app.metrics["requests_error"] += 1
            _app._vllm_breaker.record_failure()
            log.warning(
                "PASSTHROUGH-STREAM session=%s error: %s",
                session_key, e)
            _app._record_call(session_key, input_tokens, 0,
                         "error", VLLM_MODEL, True, str(e)[:300])
            yield "data: " + json.dumps(
                {"error": str(e)[:200]}) + "\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(),
                             media_type="text/event-stream")


