#!/usr/bin/env python3
import time, json, urllib.request

# Natural mixed content: prose + code + JSON
prose = 'The system uses a rolling window approach to manage context. When the input exceeds the maximum, the oldest messages are trimmed. This ensures that the most recent conversation turns are always preserved for the model. The calibration process compares estimated token counts against real counts from the inference engine.\n\n'
code = 'def calculate_tokens(messages):\n    total = 0\n    for msg in messages:\n        content = msg.get("content", "")\n        total += len(content) // 4\n        total += 8  # overhead\n    return total\n\n'
js = json.dumps({'function': 'search', 'args': {'query': 'rolling window context management', 'limit': 10, 'filters': {'type': 'document', 'language': 'en'}}})
combined = (prose + code + js) * 30  # ~20k chars

body = json.dumps({
    'model': 'Qwen3.8-27B',
    'messages': [{'role': 'user', 'content': combined}],
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
print(f'chars={len(combined)} total_time={(t1-t0)*1000:.0f}ms prompt_tokens={usage.get("prompt_tokens")}')
print(f'chars_per_token={len(combined)/usage.get("prompt_tokens",1):.2f}')
