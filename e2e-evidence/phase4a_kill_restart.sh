#!/bin/bash
cd /home/pawelw/ctxproxy/e2e-evidence

echo "=== PHASE 4a: KILL & RESTART ===" > phase4a-output.txt
echo "Timestamp: $(date)" >> phase4a-output.txt

# Test 1: SIGTERM
echo "--- Test 1: SIGTERM ---" >> phase4a-output.txt
UVICORN_PID=$(pgrep -f "uvicorn.*9201" | head -1)
echo "Killing PID $UVICORN_PID with SIGTERM" >> phase4a-output.txt
kill $UVICORN_PID 2>/dev/null
sleep 2
echo "Restarting proxy..." >> phase4a-output.txt
systemctl --user restart ctxgate-proxy
sleep 3
HEALTH=$(curl -s -w "\nHTTP:%{http_code}" http://localhost:9201/health)
echo "Health after SIGTERM restart: $HEALTH" >> phase4a-output.txt
RECOVERY=$(curl -s -w "\nHTTP:%{http_code}" http://localhost:9201/v1/chat/completions -H "Content-Type: application/json" -H "X-Session-ID: e2e-restart-001" -d '{"model":"qwen","messages":[{"role":"user","content":"Are you back?"}],"max_tokens":20}')
echo "Recovery request: $RECOVERY" >> phase4a-output.txt

# Test 2: SIGKILL
echo "--- Test 2: SIGKILL ---" >> phase4a-output.txt
UVICORN_PID=$(pgrep -f "uvicorn.*9201" | head -1)
echo "Killing PID $UVICORN_PID with SIGKILL" >> phase4a-output.txt
kill -9 $UVICORN_PID 2>/dev/null
sleep 2
echo "Restarting proxy after SIGKILL..." >> phase4a-output.txt
systemctl --user restart ctxgate-proxy
sleep 3
HEALTH=$(curl -s -w "\nHTTP:%{http_code}" http://localhost:9201/health)
echo "Health after SIGKILL restart: $HEALTH" >> phase4a-output.txt
RECOVERY=$(curl -s -w "\nHTTP:%{http_code}" http://localhost:9201/v1/chat/completions -H "Content-Type: application/json" -H "X-Session-ID: e2e-restart-001" -d '{"model":"qwen","messages":[{"role":"user","content":"Still here?"}],"max_tokens":20}')
echo "Recovery request: $RECOVERY" >> phase4a-output.txt

echo "=== PHASE 4a COMPLETE ===" >> phase4a-output.txt
cat phase4a-output.txt