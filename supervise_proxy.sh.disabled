#!/bin/bash
# ctxgate-proxy supervisor: restarts the proxy if it crashes
set +H
cd /home/user/ctxproxy

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting ctxgate-proxy..." >> .proxy_restart.log
    export $(grep -v '^#' .env | grep -v '^$' | xargs)
    python -u proxy/app.py >> proxy.log 2>&1
    EXIT_CODE=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Proxy exited with code $EXIT_CODE. Restarting in 2s..." >> .proxy_restart.log
    sleep 2
done
