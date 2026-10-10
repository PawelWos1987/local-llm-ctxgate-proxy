#!/bin/bash
cd /home/pawelw/ctxproxy
export $(grep -v "^#" .env | xargs)
export CTXGATE_PROXY_PORT=19203
export CTXGATE_DB_DSN="postgresql://postgres:${CTXGATE_PG_PASS}@127.0.0.1:5432/ctxproxy_sandbox"
export CTXGATE_ALLOW_NO_AUTH=1
export CTXGATE_HOST=127.0.0.1
export CTXGATE_MAX_CONTEXT=200
export CTXGATE_MAX_INPUT=100
export CTXGATE_MAX_OUTPUT=50
export CTXGATE_SAFETY_MARGIN=20
export CTXGATE_MIN_OUTPUT=30
nohup python3 /home/pawelw/ctxproxy/scratch/app_sandbox.py > /home/pawelw/ctxproxy/scratch/sandbox_proxy.log 2>&1 &
SANDBOX_PID=$!
echo $SANDBOX_PID > /home/pawelw/ctxproxy/scratch/sandbox_proxy.pid
echo "Started PID: $SANDBOX_PID"
sleep 3
if kill -0 $SANDBOX_PID 2>/dev/null; then
    echo "RUNNING"
    tail -5 /home/pawelw/ctxproxy/scratch/sandbox_proxy.log
else
    echo "DIED"
    cat /home/pawelw/ctxproxy/scratch/sandbox_proxy.log
fi