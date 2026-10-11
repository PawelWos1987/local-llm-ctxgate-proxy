#!/usr/bin/env python3
import time, json, urllib.request

# Send 5 requests with the same content
msg = 'Hello, this is a test message for parity checking. ' * 100
for i in range(5):
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
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    t1 = time.monotonic()
    usage = data.get('usage', {})
    status = resp.status
    content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
    print(f'req{i+1}: status={status} time={(t1-t0)*1000:.0f}ms prompt_tokens={usage.get("prompt_tokens")} content_len={len(content) if content else 0}')
