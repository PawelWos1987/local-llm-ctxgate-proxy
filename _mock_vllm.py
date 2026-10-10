#!/usr/bin/env python3
"""Mock vLLM server for testing the retry tool-call flush path."""
import json
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

CALL_COUNT = {"n": 0}

class MockVLLMHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence
    
    def do_GET(self):
        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"object": "list", "data": [{"id": "Qwen3.8-27B", "object": "model"}]}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        CALL_COUNT["n"] += 1
        is_stream = body.get("stream", False)
        enable_thinking = body.get("chat_template_kwargs", {}).get("enable_thinking", True)
        
        print(f"[MOCK-VLLM] Call #{CALL_COUNT['n']} stream={is_stream} thinking={enable_thinking}", flush=True)
        
        if CALL_COUNT["n"] == 1:
            # First call (thinking): long reasoning, no content, no tool calls
            reasoning = "Let me think about this carefully. " * 15  # ~480 chars
            if is_stream:
                self._stream_response(reasoning=reasoning, content="", tool_calls=None)
            else:
                self._json_response(reasoning=reasoning, content="", tool_calls=None)
        else:
            # Retry call (non-thinking): execute_typescript tool call
            tc = [{"index": 0, "id": "call_test_1", "type": "function",
                   "function": {"name": "execute_typescript", "arguments": json.dumps({"code": "async function run() { return 42; }"})}}]
            if is_stream:
                self._stream_response(reasoning="", content="", tool_calls=tc)
            else:
                self._json_response(reasoning="", content="", tool_calls=tc)
    
    def _stream_response(self, reasoning, content, tool_calls):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        
        stream_id = "mock-stream-1"
        model = "Qwen3.8-27B"
        
        # Reasoning chunks
        if reasoning:
            for i in range(0, len(reasoning), 50):
                chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0,
                         "model": model, "choices": [{"index": 0, "delta": {"reasoning_content": reasoning[i:i+50]}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        
        # Content chunks
        if content:
            chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0,
                     "model": model, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        
        # Tool call chunks
        if tool_calls:
            for tc in tool_calls:
                chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0,
                         "model": model, "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        
        # Final chunk with finish_reason
        finish = "tool_calls" if tool_calls else "stop"
        chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0,
                 "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
    
    def _json_response(self, reasoning, content, tool_calls):
        msg = {}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if content:
            msg["content"] = content
        else:
            msg["content"] = None
        if tool_calls:
            msg["tool_calls"] = tool_calls
        
        resp = {"id": "mock-1", "object": "chat.completion", "created": 0,
                "model": "Qwen3.8-27B",
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 9303), MockVLLMHandler)
    print("[MOCK-VLLM] Listening on 127.0.0.1:9303", flush=True)
    server.serve_forever()
