#!/usr/bin/env python3
"""Mock vLLM upstream for benchmarking. Serves SSE streaming responses."""
import asyncio
import json
import time
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    stream = body.get("stream", False)
    max_tokens = body.get("max_tokens", 100)
    
    if not stream:
        return {
            "id": "mock-1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "qwen",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello! " * 20}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        }
    
    async def generate():
        n_chunks = min(200, max_tokens * 2)
        for i in range(n_chunks):
            chunk = {
                "id": "mock-1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "qwen",
                "choices": [{"index": 0, "delta": {"content": f"word{i} "}, "finish_reason": None}],
            }
            if i == n_chunks - 1:
                chunk["choices"][0]["delta"] = {}
                chunk["choices"][0]["finish_reason"] = "stop"
                chunk["usage"] = {"prompt_tokens": 100, "completion_tokens": n_chunks, "total_tokens": 100 + n_chunks}
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.001)  # 1ms between chunks for faster benchmarking
        yield "data: [DONE]\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9298, log_level="warning")
