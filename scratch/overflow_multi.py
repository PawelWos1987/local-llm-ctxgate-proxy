#!/usr/bin/env python3
import time, json, urllib.request

# Create 20 messages, each ~3500 tokens worth of content
# 3500 tokens * 4.1 chars/token = ~14350 chars per message
# 20 * 14350 = ~287000 chars total = ~70k tokens
msgs = []
for i in range(20):
    msgs.append({'role': 'user' if i % 2 == 0 else 'assistant',
                 'content': 'Message %d: ' % i + 'The system processes data in batches. ' * 200})

body = json.dumps({
    'model': 'Qwen3.8-27B',
    'messages': msgs,
    'max_tokens': 50
}).encode()

total_chars = sum(len(m['content']) for m in msgs)
print('total_chars=%d est_tokens~=%d' % (total_chars, total_chars // 4))

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
    content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
    print('status=%d time=%.0fms prompt_tokens=%d content_len=%d' % (
        resp.status, (t1-t0)*1000, usage.get('prompt_tokens', 0), len(content) if content else 0))
except Exception as e:
    t1 = time.monotonic()
    print('error: %s time=%.0fms' % (e, (t1-t0)*1000))
