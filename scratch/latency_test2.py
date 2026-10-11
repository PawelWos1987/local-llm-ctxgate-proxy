#!/usr/bin/env python3
import time, json, urllib.request

# Test with ~40k chars (close to real Goose usage)
prose = 'The system manages context windows using a rolling approach. When input exceeds the maximum budget, older messages are trimmed to make room for new content. The calibration process ensures that token estimates remain accurate across different content types including prose, code, and structured data.\n\n'
code = 'import asyncio\nfrom typing import List, Dict\n\nasync def process_messages(msgs: List[Dict]) -> int:\n    total = 0\n    for m in msgs:\n        c = m.get("content", "")\n        total += len(c) // 4 + 10\n    return total\n\n'
combined = (prose + code) * 200  # ~40k chars

body = json.dumps({
    'model': 'Qwen3.8-27B',
    'messages': [{'role': 'user', 'content': combined}],
    'max_tokens': 10
}).encode()

req = urllib.request.Request(
    'http://127.0.0.1:19201/v1/chat/completions',
    data=body,
    headers={'Content-Type': 'application/json'}
)

t0 = time.monotonic()
resp = urllib.request.urlopen(req, timeout=60)
data = json.loads(resp.read())
t1 = time.monotonic()

usage = data.get('usage', {})
print(f'chars={len(combined)} total_time={(t1-t0)*1000:.0f}ms prompt_tokens={usage.get("prompt_tokens")}')
print(f'chars_per_token={len(combined)/usage.get("prompt_tokens",1):.2f}')
