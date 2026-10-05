#!/usr/bin/env python3
"""Mock vLLM server for testing ctxgate-proxy. Supports tools, large outputs, errors."""
import json, time, uuid, argparse
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 29100
request_log = []

def estimate_tokens(text):
    return max(1, len(text) // 4)

class MockVLLM(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass
    
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        
        messages = body.get("messages", [])
        tools = body.get("tools", [])
        max_tokens = body.get("max_tokens", 18000)
        stream = body.get("stream", False)
        
        # Find marker in last user message
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str):
                    last_user = c
                break
        
        input_tokens = estimate_tokens(json.dumps(messages))
        
        # Determine response type
        resp_type = "normal"
        if "[TOOL_SIMPLE]" in last_user:
            resp_type = "tool_simple"
        elif "[TOOL_BIG]" in last_user:
            resp_type = "tool_big"
        elif "[TOOL_MULTI]" in last_user:
            resp_type = "tool_multi"
        elif "[TOOL_CHAIN]" in last_user:
            resp_type = "tool_chain"
        elif "[TOOL_TRUNC]" in last_user:
            resp_type = "tool_trunc"
        elif "[OUT_40K]" in last_user:
            resp_type = "out_40k"
        elif "[OUT_25K]" in last_user:
            resp_type = "out_25k"
        elif "[OUT_18K]" in last_user:
            resp_type = "out_18k"
        elif "[ERR_400]" in last_user:
            resp_type = "err_400"
        elif "[ERR_500]" in last_user:
            resp_type = "err_500"
        
        # Check for tool results in history (for chain)
        tool_results = [m for m in messages if m.get("role") == "tool"]
        
        request_log.append({
            "ts": time.time(),
            "max_tokens": max_tokens,
            "num_messages": len(messages),
            "has_tools": bool(tools),
            "input_tokens": input_tokens,
            "type": resp_type,
        })
        
        if resp_type == "err_400":
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "max_tokens exceeds context"}}).encode())
            return
        
        if resp_type == "err_500":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "internal error"}}).encode())
            return
        
        # Build response
        content = ""
        tool_calls = None
        finish_reason = "stop"
        reasoning_content = ""
        
        if resp_type == "tool_simple":
            tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "/tmp/test.txt", "content": "hello world"})}}]
            finish_reason = "tool_calls"
        elif resp_type == "tool_big":
            big_content = "A" * 10000
            tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "/tmp/big.txt", "content": big_content})}}]
            finish_reason = "tool_calls"
        elif resp_type == "tool_multi":
            tool_calls = [
                {"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "/tmp/a.txt", "content": "a"})}},
                {"id": "call_2", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "/tmp/b.txt", "content": "b"})}},
                {"id": "call_3", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "/tmp/c.txt", "content": "c"})}},
            ]
            finish_reason = "tool_calls"
        elif resp_type == "tool_chain":
            if len(tool_results) >= 2:
                content = "All files written successfully."
                finish_reason = "stop"
            else:
                tool_calls = [{"id": "call_2", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "/tmp/second.txt", "content": "second file"})}}]
                finish_reason = "tool_calls"
        elif resp_type == "tool_trunc":
            tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "/tmp/trunc.txt", "content": "this is cut off mid-'}}]
            finish_reason = "length"
        elif resp_type == "out_40k":
            content = "The quick brown fox jumps over the lazy dog. " * 2000
            finish_reason = "stop"
        elif resp_type == "out_25k":
            content = "The quick brown fox jumps over the lazy dog. " * 1250
            finish_reason = "stop"
        elif resp_type == "out_18k":
            content = "The quick brown fox jumps over the lazy dog. " * 900
            finish_reason = "stop"
        else:
            content = "This is a normal short response. The proxy is working correctly."
            finish_reason = "stop"
        
        output_tokens = estimate_tokens(content) + (len(tool_calls) * 10 if tool_calls else 0)
        
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            
            # Send content in chunks
            if content:
                chunk_size = 200
                for i in range(0, len(content), chunk_size):
                    piece = content[i:i+chunk_size]
                    chunk = {"id": "gen", "object": "chat.completion.chunk", "created": int(time.time()), "model": "Qwen3.8-27B", "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            
            # Send tool_calls
            if tool_calls:
                for idx, tc in enumerate(tool_calls):
                    tc_chunk = {"id": "gen", "object": "chat.completion.chunk", "created": int(time.time()), "model": "Qwen3.8-27B", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": idx, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]}
                    self.wfile.write(b"data: " + json.dumps(tc_chunk).encode() + b"\n\n")
            
            # Final chunk with finish_reason
            final = {"id": "gen", "object": "chat.completion.chunk", "created": int(time.time()), "model": "Qwen3.8-27B", "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}], "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}}
            self.wfile.write(b"data: " + json.dumps(final).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            msg = {"role": "assistant", "content": content if content else None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if reasoning_content:
                msg["reasoning_content"] = reasoning_content
            
            resp = {
                "id": "gen-" + uuid.uuid4().hex[:8],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "Qwen3.8-27B",
                "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        elif self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "Qwen3.8-27B", "object": "model"}]}).encode())
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=29100)
    args = parser.parse_args()
    print(f"Mock vLLM server running on port {args.port}", flush=True)
    server = HTTPServer(("127.0.0.1", args.port), MockVLLM)
    server.serve_forever()
