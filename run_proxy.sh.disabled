#!/bin/bash
# ctxgate-proxy supervisor: auto-restart on crash
set +H
cd /home/user/ctxproxy
export $(grep -v '^#' .env | grep -v '^$' | xargs)

while true; do
    python3 -u proxy/app.py >> proxy.log 2>&1
    EXIT_CODE=$?
    echo "$(date '+%Y-%m-%d %H:%M:%S') [supervisor] proxy exited with code $EXIT_CODE, restarting in 3s..." >> proxy.log
    sleep 3
done
