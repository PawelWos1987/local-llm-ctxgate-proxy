#!/usr/bin/env python3
"""P1.0: Verify /tokenize parity with usage.prompt_tokens."""
import json, httpx, time

VLLM = "http://127.0.0.1:29000"

def tokenize(messages, tools=None):
    body = {"model": "Qwen3.8-27B", "messages": messages, "add_generation_prompt": True}
    if tools:
        body["tools"] = tools
    r = httpx.post(f"{VLLM}/tokenize", json=body, timeout=30)
    r.raise_for_status()
    return r.json()["count"]

def completions(messages, tools=None):
    body = {
        "model": "Qwen3.8-27B",
        "messages": messages,
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
    r = httpx.post(f"{VLLM}/v1/chat/completions", json=body, timeout=30)
    r.raise_for_status()
    return r.json()["usage"]["prompt_tokens"]

# Test samples
samples = [
    ([{"role": "user", "content": "Hello world"}], None, "tiny"),
    ([{"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is the capital of France?"}], None, "sys+user"),
    ([{"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Write a Python function to sort a list."},
      {"role": "assistant", "content": "Here's a function:\ndef sort_list(lst):\n    return sorted(lst)\nThis uses Python's built-in sorted() which is O(n log n)."},
      {"role": "user", "content": "Can you make it use merge sort instead?"}], None, "4-msg conversation"),
    ([{"role": "user", "content": "Tell me about quantum computing in detail. Cover qubits, superposition, entanglement, and major algorithms like Shor's and Grover's. Explain the practical applications and current limitations."}], None, "long single"),
    ([{"role": "system", "content": "You are a code assistant."},
      {"role": "user", "content": "Write a FastAPI endpoint that handles file uploads"},
      {"role": "assistant", "content": "from fastapi import FastAPI, UploadFile\nfrom fastapi.responses import FileResponse\n\napp = FastAPI()\n\n@app.post('/upload')\nasync def upload(file: UploadFile):\n    contents = await file.read()\n    return {'size': len(contents)}"},
      {"role": "user", "content": "Add validation for file type and size"},
      {"role": "assistant", "content": "from fastapi import FastAPI, UploadFile, HTTPException\nimport os\n\napp = FastAPI()\nALLOWED = {'image/png', 'image/jpeg', 'application/pdf'}\nMAX_SIZE = 10 * 1024 * 1024\n\n@app.post('/upload')\nasync def upload(file: UploadFile):\n    if file.content_type not in ALLOWED:\n        raise HTTPException(400, 'Invalid file type')\n    contents = await file.read()\n    if len(contents) > MAX_SIZE:\n        raise HTTPException(400, 'File too large')\n    return {'size': len(contents)}"},
      {"role": "user", "content": "Now add a database layer to store the file metadata"}], None, "6-msg code"),
    # With tools
    ([{"role": "user", "content": "What's the weather in Paris?"}],
     [{"type": "function", "function": {"name": "get_weather", "description": "Get current weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
     "with-tools-1"),
    ([{"role": "system", "content": "You are a helpful assistant with access to tools."},
      {"role": "user", "content": "Search for the latest news about AI and summarize the top 3 stories."}],
     [{"type": "function", "function": {"name": "web_search", "description": "Search the web", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 10}}, "required": ["query"]}}},
      {"type": "function", "function": {"name": "read_url", "description": "Read content from a URL", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}}],
     "with-tools-2"),
]

print("P1.0: /tokenize vs /v1/chat/completions parity")
print("=" * 60)
all_pass = True
for msgs, tools, label in samples:
    t_tok = tokenize(msgs, tools)
    t_comp = completions(msgs, tools)
    diff = abs(t_tok - t_comp)
    pct = diff / max(1, t_comp) * 100
    status = "PASS" if diff <= 2 else "FAIL"
    if diff > 2:
        all_pass = False
    print(f"{label:20s} tokenize={t_tok:6d} completions={t_comp:6d} diff={diff:3d} ({pct:.1f}%) {status}")

print(f"\nOverall: {'PASS' if all_pass else 'FAIL'}")
print(f"TOKENIZE_MODE: vllm_tokenize" if all_pass else "TOKENIZE_MODE: fallback_needed")
