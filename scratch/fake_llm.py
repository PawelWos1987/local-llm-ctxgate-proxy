#!/usr/bin/env python3
"""Fake OpenAI-compatible LLM server for sandbox testing. Returns instant canned replies."""
import json
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 19204

class FakeLLMHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress logging

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            stream = body.get("stream", False)
            
            if stream:
                self._stream_response(body)
            else:
                self._nonstream_response(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _stream_response(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        
        cid = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        ts = int(time.time())
        
        # First chunk: role
        chunk1 = {"id": cid, "object": "chat.completion.chunk", "created": ts,
                  "model": "fake-llm", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
        self.wfile.write(f"data: {json.dumps(chunk1)}\n\n".encode())
        
        # Content chunk
        chunk2 = {"id": cid, "object": "chat.completion.chunk", "created": ts,
                  "model": "fake-llm", "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]}
        self.wfile.write(f"data: {json.dumps(chunk2)}\n\n".encode())
        
        # Final chunk
        chunk3 = {"id": cid, "object": "chat.completion.chunk", "created": ts,
                  "model": "fake-llm", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(chunk3)}\n\n".encode())
        
        self.wfile.write(b"data: [DONE]\n\n")

    def _nonstream_response(self, body):
        cid = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        ts = int(time.time())
        resp = {"id": cid, "object": "chat.completion", "created": ts,
                "model": "fake-llm",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), FakeLLMHandler)
    print(f"Fake LLM server on port {PORT}", flush=True)
    server.serve_forever()

