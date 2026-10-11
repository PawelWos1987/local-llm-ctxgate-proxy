#!/usr/bin/env python3
import time, sys
sys.path.insert(0, '/home/pawelw/ctxproxy')
from proxy.app import count_messages_tokens

msgs = [{'role': 'user', 'content': 'The quick brown fox jumps over the lazy dog. ' * 10000}]
t0 = time.monotonic()
est = count_messages_tokens(msgs)
t1 = time.monotonic()
chars = len(msgs[0]['content'])
print('chars=%d est=%d time=%.3fms' % (chars, est, (t1-t0)*1000))
print('PASS: < 100ms' if (t1-t0)*1000 < 100 else 'FAIL: >= 100ms')
