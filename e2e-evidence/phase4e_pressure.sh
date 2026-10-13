#!/bin/bash
cd /home/pawelw/ctxproxy/e2e-evidence

echo "=== PHASE 4e: RESOURCE PRESSURE ===" > phase4e-output.txt
echo "Starting 50 concurrent requests..." >> phase4e-output.txt

BEFORE_FD=$(ls /proc/$(pgrep -f "uvicorn.*9201" | head -1)/fd 2>/dev/null | wc -l)
echo "FD count before: $BEFORE_FD" >> phase4e-output.txt

seq 1 50 | xargs -P 50 -I{} curl -s -o /dev/null -w "%{http_code}\n" \
  http://localhost:9201/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: e2e-pressure-{}" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' >> phase4e-codes.txt 2>&1

AFTER_FD=$(ls /proc/$(pgrep -f "uvicorn.*9201" | head -1)/fd 2>/dev/null | wc -l)
echo "FD count after: $AFTER_FD" >> phase4e-output.txt

SUCCESS_COUNT=$(grep -c "^200$" phase4e-codes.txt 2>/dev/null || echo 0)
FAIL_COUNT=$(wc -l < phase4e-codes.txt 2>/dev/null || echo 0)
echo "Success: $SUCCESS_COUNT/50, Total responses: $FAIL_COUNT" >> phase4e-output.txt

HEALTH=$(curl -s -w "\nHTTP:%{http_code}" http://localhost:9201/health)
echo "Post-stress health: $HEALTH" >> phase4e-output.txt

echo "=== PHASE 4e COMPLETE ===" >> phase4e-output.txt
cat phase4e-output.txt