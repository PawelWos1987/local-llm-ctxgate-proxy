#!/usr/bin/env python3
import time, json, urllib.request

msg = 'The quick brown fox jumps over the lazy dog. ' * 500
body = json.dumps({
    'model': 'Qwen3.8-27B',
    'messages': [{'role': 'user', 'content': msg}],
    'max_tokens': 20
}).encode()

req = urllib.request.Request(
    'http://127.0.0.1:19201/v1/chat/completions',
    data=body,
    headers={'Content-Type': 'application/json'}
)

t0 = time.monotonic()
resp = urllib.request.urlopen(req, timeout=30)
data = json.loads(resp.read())
t1 = time.monotonic()

usage = data.get('usage', {})
content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
print(f'Total time: {(t1-t0)*1000:.0f}ms')
print(f'prompt_tokens: {usage.get("prompt_tokens")}')
print(f'completion_tokens: {usage.get("completion_tokens")}')
print(f'content_len: {len(content)}')
