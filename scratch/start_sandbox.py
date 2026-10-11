#!/usr/bin/env python3
import subprocess, os, sys, time

env = os.environ.copy()
env.update({
    'CTXGATE_SKIP_DOTENV': '1',
    'CTXGATE_PROXY_PORT': '19201',
    'CTXGATE_VLLM_URL': 'http://127.0.0.1:29000/v1',
    'CTXGATE_VLLM_MODEL': 'Qwen3.8-27B',
    'CTXGATE_MAX_CONTEXT': '84000',
    'CTXGATE_MAX_INPUT': '64000',
    'CTXGATE_MAX_OUTPUT': '16000',
    'CTXGATE_SAFETY_MARGIN': '4000',
    'CTXGATE_MIN_OUTPUT': '8000',
    'CTXGATE_DB_DSN': 'postgresql://postgres:11!!AdaMicPaw@127.0.0.1:5432/ctxproxy_sandbox',
    'CTXGATE_API_KEY': 'test-key-123',
    'CTXGATE_ALLOW_NO_AUTH': '1',
    'CTXGATE_QWEN_TOKENIZER': '/home/pawelw/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json',
})

log = open('/home/pawelw/ctxproxy/scratch/sandbox-proxy.log', 'w')
p = subprocess.Popen(
    [sys.executable, '/home/pawelw/ctxproxy/proxy/app.py'],
    env=env,
    stdout=log,
    stderr=subprocess.STDOUT,
    cwd='/home/pawelw/ctxproxy',
)
print(f'Started PID {p.pid}')
time.sleep(15)
if p.poll() is not None:
    print(f'Process died with code {p.returncode}')
    log.close()
    with open('/home/pawelw/ctxproxy/scratch/sandbox-proxy.log') as f:
        print(f.read()[:2000])
else:
    print(f'Process alive, PID {p.pid}')
