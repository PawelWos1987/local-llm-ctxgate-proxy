#!/bin/bash
# ctxgate-proxy supervisor - with lock file to prevent double-start
LOG=/home/user/ctxproxy/proxy.log
PIDFILE=/home/user/ctxproxy/proxy.pid
LOCKFILE=/home/user/ctxproxy/supervisor.lock
cd /home/user/ctxproxy

export $(grep -v '#' .env | grep -v '^$' | xargs)

# Lock file guard - prevents double-start
if [ -f "$LOCKFILE" ]; then
    OLD_PID=$(cat "$LOCKFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date)] [supervisor] already running PID=$OLD_PID, exiting" >> "$LOG"
        exit 0
    fi
fi
echo $$ > "$LOCKFILE"

# Kill any stale process on port 9200 at startup
STALE=$(lsof -ti:9200 2>/dev/null)
if [ -n "$STALE" ]; then
    echo "[$(date)] [supervisor] killing stale PID=$STALE on port 9200" >> "$LOG"
    kill -9 $STALE 2>/dev/null
    sleep 0.5
fi

CONSECUTIVE_FAILURES=0

while true; do
    python3 -u proxy/app.py >> "$LOG" 2>&1 &
    PID=$!
    echo $PID > "$PIDFILE"
    echo "[$(date)] [supervisor] started proxy PID=$PID" >> "$LOG"
    START_TIME=$(date +%s)
    wait $PID
    EXIT_CODE=$?
    END_TIME=$(date +%s)
    RUN_TIME=$((END_TIME - START_TIME))

    if [ $EXIT_CODE -eq 0 ]; then
        CONSECUTIVE_FAILURES=0
        sleep 1
    else
        if [ $RUN_TIME -gt 5 ]; then
            CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
            if [ $CONSECUTIVE_FAILURES -ge 3 ]; then
                BACKOFF=10
            else
                BACKOFF=2
            fi
        else
            CONSECUTIVE_FAILURES=0
            BACKOFF=1
        fi
        echo "[$(date)] [supervisor] proxy exited code=$EXIT_CODE after ${RUN_TIME}s, retrying in ${BACKOFF}s" >> "$LOG"
        sleep $BACKOFF
    fi
done
