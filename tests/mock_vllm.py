#!/usr/bin/env python3
"""Mock vLLM SSE server for Phase 1b characterization tests."""
import argparse, json, sys, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

SCENARIO = {"name": "a", "call_count": 0}
LOCK = threading.Lock()

def _sse(chunks):
    parts = ["data: " + json.dumps(c) + "\n\n" for c in chunks]
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")

def _chunk(cid, delta, fr=None, usage=None):
    c = {"id": cid, "object": "chat.completion.chunk", "created": 0,
         "model": "mock-model",
         "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}
    if usage:
        c["usage"] = usage
    return c

def _a():
    cid = "mock-a"
    return _sse([
        _chunk(cid, {"role": "assistant", "content": "I will read the file now."}),
        _chunk(cid, {"tool_calls": [{"index": 0, "id": "call_001", "type": "function",
                                     "function": {"name": "read_file", "arguments": ""}}]}),
        _chunk(cid, {"tool_calls": [{"index": 0, "id": "call_001", "type": "function",
                                     "function": {"name": "", "arguments": "{\"path\": \"/tmp/test.txt\"}"}}]}),
        _chunk(cid, {}, "tool_calls", {"completion_tokens": 30, "prompt_tokens": 100}),
    ])

def _b():
    with LOCK:
        SCENARIO["call_count"] += 1
        n = SCENARIO["call_count"]
    if n == 1:
        cid = "mock-b1"
        # Non-repeating text, ~400 chars
        text = ("The project structure consists of three main modules. "
                "The first module handles data ingestion from various sources. "
                "The second module processes the data through a pipeline. "
                "The third module serves the results via an API endpoint. "
                "Each module has its own configuration file and test suite. "
                "The data flows from ingestion through processing to serving.")
        return _sse([
            _chunk(cid, {"role": "assistant", "content": text}),
            _chunk(cid, {}, "length", {"completion_tokens": 120, "prompt_tokens": 100}),
        ])
    else:
        cid = "mock-b2"
        # Continuation with seam overlap
        overlap = "to serving."
        new_text = " The overall architecture follows a clean separation of concerns."
        return _sse([
            _chunk(cid, {"role": "assistant", "content": overlap + new_text}),
            _chunk(cid, {}, "stop", {"completion_tokens": 40, "prompt_tokens": 200}),
        ])

def _c():
    with LOCK:
        SCENARIO["call_count"] += 1
        n = SCENARIO["call_count"]
    if n == 1:
        cid = "mock-c1"
        # Lots of reasoning, no content, finish=length
        reasoning = ("Let me analyze the problem step by step. First, I need to understand the requirements. "
                     "The user wants a summary of the project status. I should look at the files that were "
                     "created recently and check the test results. Let me think about what to include in the "
                     "summary. The key deliverables are the FMEA documents and the implementation plan. "
                     "I should also mention the test coverage and any remaining issues.")
        return _sse([
            _chunk(cid, {"role": "assistant", "reasoning_content": reasoning}),
            _chunk(cid, {}, "length", {"completion_tokens": 200, "prompt_tokens": 100}),
        ])
    else:
        cid = "mock-c2"
        return _sse([
            _chunk(cid, {"role": "assistant", "content": "Here is the summary."}),
            _chunk(cid, {"tool_calls": [{"index": 0, "id": "call_c1", "type": "function",
                                         "function": {"name": "write_file", "arguments": ""}}]}),
            _chunk(cid, {"tool_calls": [{"index": 0, "id": "call_c1", "type": "function",
                                         "function": {"name": "", "arguments": "{\"path\": \"/tmp/out.txt\", \"content\": \"hello\"}"}}]}),
            _chunk(cid, {}, "tool_calls", {"completion_tokens": 50, "prompt_tokens": 100}),
        ])

def _d():
    cid = "mock-d"
    # Repeating pattern in reasoning to trigger _detect_loop
    pattern = "checking the data and verifying the results "
    reasoning = pattern * 20
    return _sse([
        _chunk(cid, {"role": "assistant", "reasoning_content": reasoning}),
        _chunk(cid, {}, "stop", {"completion_tokens": 200, "prompt_tokens": 100}),
    ])

def _e():
    cid = "mock-e"
    # Repeating pattern in content (no tool call)
    pattern = "This is a sentence that repeats over and over again. "
    content = pattern * 20
    return _sse([
        _chunk(cid, {"role": "assistant", "content": content}),
        _chunk(cid, {}, "stop", {"completion_tokens": 300, "prompt_tokens": 100}),
    ])

def _e2():
    cid = "mock-e2"
    # Complete tool call, then content loop would be detected on next call
    # But for simplicity, just return a complete tool call
    return _sse([
        _chunk(cid, {"role": "assistant", "content": "Reading the file."}),
        _chunk(cid, {"tool_calls": [{"index": 0, "id": "call_e2", "type": "function",
                                     "function": {"name": "read_file", "arguments": "{\"path\": \"/tmp/x\"}"}}]}),
        _chunk(cid, {}, "tool_calls", {"completion_tokens": 30, "prompt_tokens": 100}),
    ])

def _f():
    cid = "mock-f"
    # Tool call truncated: arguments JSON is incomplete
    return _sse([
        _chunk(cid, {"role": "assistant", "content": "Writing the file."}),
        _chunk(cid, {"tool_calls": [{"index": 0, "id": "call_f1", "type": "function",
                                     "function": {"name": "write_file", "arguments": "{\"path\": \"/tmp/incomplete"}}]}),
        _chunk(cid, {}, "length", {"completion_tokens": 50, "prompt_tokens": 100}),
    ])

def _g():
    cid = "mock-g"
    # Interrupted: send content, no finish_reason, no [DONE]
    parts = []
    parts.append("data: " + json.dumps(_chunk(cid, {"role": "assistant", "content": "This is some content that will be "})) + "\n\n")
    parts.append("data: " + json.dumps(_chunk(cid, {"content": "interrupted mid-stream."})) + "\n\n")
    return "".join(parts).encode("utf-8")

def _h():
    with LOCK:
        SCENARIO["call_count"] += 1
        n = SCENARIO["call_count"]
    if n == 1:
        cid = "mock-h1"
        pattern = "analyzing the problem space carefully "
        reasoning = pattern * 25
        return _sse([
            _chunk(cid, {"role": "assistant", "reasoning_content": reasoning}),
            _chunk(cid, {}, "stop", {"completion_tokens": 200, "prompt_tokens": 100}),
        ])
    else:
        cid = "mock-h2"
        # Retry also hits finish=length
        text = ("The analysis reveals several key findings about the system. "
                "The first finding is that the data pipeline has a bottleneck. "
                "The second finding is that the API response times are within acceptable limits. "
                "The third finding is that the test coverage needs improvement in the edge cases.")
        return _sse([
            _chunk(cid, {"role": "assistant", "content": text}),
            _chunk(cid, {}, "length", {"completion_tokens": 150, "prompt_tokens": 100}),
        ])

SCENARIOS = {"a": _a, "b": _b, "c": _c, "d": _d, "e": _e, "e2": _e2, "f": _f, "g": _g, "h": _h}

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/chat/completions":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req = json.loads(body)
            scenario = req.get("mock_scenario", "a")
            resp_body = SCENARIOS.get(scenario, _a)()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(resp_body)
        elif self.path == "/reset":
            with LOCK:
                SCENARIO["call_count"] = 0
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, fmt, *args):
        pass

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9300)
    args = p.parse_args()
    srv = HTTPServer(("127.0.0.1", args.port), H)
    print("Mock vLLM on %d" % args.port, flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
