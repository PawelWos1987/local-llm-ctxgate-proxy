#!/usr/bin/env python3
import time, json, urllib.request

# Send a request with content that's close to the 64000 token limit
# 64000 tokens * 4.1 chars/token = ~262400 chars
# Let's send ~270000 chars to be slightly over
msg = 'x' * 270000
body = json.dumps({
    'model': 'Qwen3.8-27B',
    'messages': [{'role': 'user', 'content': msg}],
    'max_tokens': 10
}).encode()

req = urllib.request.Request(
    'http://127.0.0.1:19201/v1/chat/completions',
    data=body,
    headers={'Content-Type': 'application/json'}
)

t0 = time.monotonic()
try:
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    t1 = time.monotonic()
    usage = data.get('usage', {})
    print(f'status={resp.status} time={(t1-t0)*1000:.0f}ms prompt_tokens={usage.get("prompt_tokens")}')
except Exception as e:
    t1 = time.monotonic()
    print(f'error: {e} time={(t1-t0)*1000:.0f}ms')
