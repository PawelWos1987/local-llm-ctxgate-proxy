"""SSE (Server-Sent Events) helpers for streaming responses."""
import logging
import asyncio
import json

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


def _safe_truncate(text):
    """Truncate at a safe boundary (newline, sentence end, or space)."""
    if not text:
        return text
    nl = text.rfind(chr(10))
    if nl > len(text) * 0.8:
        return text[:nl]
    for i in range(len(text) - 1, max(len(text) - 200, 0), -1):
        if text[i] in ".!?)\"'":
            return text[:i + 1]
    sp = text.rfind(" ")
    if sp > len(text) * 0.8:
        return text[:sp]
    return text


def _sse_content(text: str, chunk_id: str = "gen") -> str:
    """One SSE line carrying a content delta."""
    # TODO(I4): replace with AppContext
    from proxy.app import VLLM_MODEL
    return "data: " + json.dumps({
        "id": chunk_id, "object": "chat.completion.chunk", "created": 0, "model": VLLM_MODEL,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }) + "\n\n"


async def _lines_with_heartbeat(resp, interval: float):
    """Yield SSE lines from resp, or None when a heartbeat is due.

    Uses a persistent pending task so that asyncio.wait timeout never
    cancels the underlying aiter_lines() generator.
    """
    it = resp.aiter_lines()
    nxt = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({nxt}, timeout=interval)
            if not done:
                yield None
                continue
            try:
                line = nxt.result()
            except StopAsyncIteration:
                return
            yield line
            nxt = asyncio.ensure_future(it.__anext__())
    finally:
        if not nxt.done():
            nxt.cancel()
            try:
                await nxt
            except (asyncio.CancelledError, Exception):
                pass


