#!/usr/bin/env python3
"""Mock vLLM SSE server for Phase 1b golden tests."""
import json
import asyncio
from aiohttp import web

async def models(request):
    return web.json_response({
        "object": "list",
        "data": [{"id": "qwen3-8b", "object": "model", "owned_by": "local"}]
    })

async def handle(request):
    body = await request.json()
    # Scenario can come from the 'scenario' field OR be embedded in the last user message
    scenario = body.get("scenario", "")
    if not scenario:
        for m in reversed(body.get("messages", [])):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str) and "scenario:" in c:
                    scenario = c.split("scenario:")[-1].strip()
                    break
    if not scenario:
        scenario = "normal"
    
    chunks = []
    
    if scenario == "normal_tool_call":
        chunks = [
            {"id": "cmpl-1", "choices": [{"delta": {"role": "assistant", "content": "Let me check that."}, "index": 0, "finish_reason": None}]},
            {"id": "cmpl-1", "choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": ""}}]}, "index": 0, "finish_reason": None}]},
            {"id": "cmpl-1", "choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "function": {"arguments": "{\"path\": \"/tmp/test.txt\"}"}}]}, "index": 0, "finish_reason": None}]},
            {"id": "cmpl-1", "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]},
        ]
    elif scenario == "text_length_continuation":
        chunks = [
            {"id": "cmpl-2", "choices": [{"delta": {"role": "assistant", "content": "Here is a very long explanation. " * 80}, "index": 0, "finish_reason": "length"}]},
        ]
    elif scenario == "reasoning_backstop":
        chunks = [
            {"id": "cmpl-3", "choices": [{"delta": {"role": "assistant", "reasoning_content": "Let me think about this carefully and consider all the options available to us in this situation. " * 30}, "index": 0, "finish_reason": "stop"}]},
        ]
    elif scenario == "reasoning_loop":
        chunks = [
            {"id": "cmpl-4", "choices": [{"delta": {"role": "assistant", "reasoning_content": "I need to check the file. I need to check the file. I need to check the file. I need to check the file. I need to check the file. I need to check the file. " * 30}, "index": 0, "finish_reason": "stop"}]},
        ]
    elif scenario == "content_loop_first":
        chunks = [
            {"id": "cmpl-5", "choices": [{"delta": {"role": "assistant", "content": "The result is 42. The result is 42. The result is 42. The result is 42. The result is 42. The result is 42. The result is 42. The result is 42. " * 15}, "index": 0, "finish_reason": "stop"}]},
        ]
    elif scenario == "content_loop_after_tool":
        chunks = [
            {"id": "cmpl-6", "choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"/tmp/x\"}"}}]}, "index": 0, "finish_reason": None}]},
            {"id": "cmpl-6", "choices": [{"delta": {"role": "assistant", "content": "The file contains data. The file contains data. The file contains data. The file contains data. The file contains data. The file contains data. The file contains data. The file contains data. " * 15}, "index": 0, "finish_reason": "stop"}]},
        ]
    elif scenario == "tool_call_truncated":
        chunks = [
            {"id": "cmpl-7", "choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_3", "type": "function", "function": {"name": "write_file", "arguments": "{\"path\": \"/tmp/long_file.txt\", \"content\": \"some very long content that gets cut off mid-JSON"}}]}, "index": 0, "finish_reason": "length"}]},
        ]
    elif scenario == "stream_interrupted":
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        await resp.prepare(request)
        chunk = {"id": "cmpl-8", "choices": [{"delta": {"role": "assistant", "content": "This is a response that will be"}, "index": 0, "finish_reason": None}]}
        await resp.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        resp.force_close()
        return resp
    elif scenario == "retry_length":
        chunks = [
            {"id": "cmpl-9", "choices": [{"delta": {"role": "assistant", "content": "A very long retry response that goes on and on and on and on and on. " * 60}, "index": 0, "finish_reason": "length"}]},
        ]
    else:
        chunks = [
            {"id": "cmpl-0", "choices": [{"delta": {"role": "assistant", "content": "Hello"}, "index": 0, "finish_reason": "stop"}]},
        ]
    
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    await resp.prepare(request)
    for chunk in chunks:
        await resp.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        await asyncio.sleep(0.01)
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp

app = web.Application()
app.router.add_get("/v1/models", models)
app.router.add_post("/v1/chat/completions", handle)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=9300)
