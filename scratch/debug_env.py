#!/usr/bin/env python3
import os, subprocess, sys, time

pgpass = subprocess.check_output("grep '^CTXGATE_PG_PASS=' /home/pawelw/ctxproxy/.env | cut -d= -f2", shell=True, text=True).strip()

env = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/home/pawelw"),
    "CTXGATE_SKIP_DOTENV": "1",
    "CTXGATE_PROXY_PORT": "19203",
    "CTXGATE_DB_DSN": f"postgresql://postgres:{pgpass}@127.0.0.1:5432/ctxproxy_sandbox",
    "CTXGATE_ALLOW_NO_AUTH": "1",
    "CTXGATE_HOST": "127.0.0.1",
    "CTXGATE_MAX_CONTEXT": "200",
    "CTXGATE_MAX_INPUT": "100",
    "CTXGATE_MAX_OUTPUT": "50",
    "CTXGATE_SAFETY_MARGIN": "20",
    "CTXGATE_MIN_OUTPUT": "30",
    "CTXGATE_VLLM_URL": "http://127.0.0.1:19204/v1",
    "CTXGATE_VLLM_MODEL": "Qwen3.8-27B",
    "CTXGATE_LM_URL": "http://127.0.0.1:19204/v1/chat/completions",
    "CTXGATE_LM_MODEL": "fake-llm",
    "CTXGATE_LM_API_KEY": "fake",
    "CTXGATE_LM_WORKERS": "2",
    "CTXGATE_QWEN_TOKENIZER": "/home/pawelw/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json",
}

# Start proxy, capture ALL output
proc = subprocess.Popen(
    [sys.executable, "/home/pawelw/ctxproxy/scratch/app_sandbox.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=env,
    cwd="/home/pawelw/ctxproxy",
)
time.sleep(4)
proc.terminate()
try:
    out, _ = proc.communicate(timeout=5)
except:
    proc.kill()
    out, _ = proc.communicate()

# Print first 20 lines
lines = out.decode().split('\n')
for line in lines[:20]:
    if line.strip():
        print(line)

