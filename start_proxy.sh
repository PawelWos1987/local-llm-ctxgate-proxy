#!/bin/bash
# Detached daemon launcher: nohup+setsid so the proxy survives shell/session
# teardown (a shell in a GUI session sends SIGTERM to its process group on exit).
set +H
cd "$(cd "$(dirname "$0")" && pwd)"

# Guard: if the systemd unit is already managing the proxy, do NOT spawn a
# duplicate. Two processes fighting over port 9201 caused the 2026-10-03
# start-limit-hit outage (the nohup instance held the port, so every systemd
# restart hit the port-lock guard and exited before READY=1).
# The systemd unit is the single source of truth.
if systemctl --user is-active --quiet ctxgate-proxy.service 2>/dev/null; then
    echo "ctxgate-proxy is already managed by systemd (ctxgate-proxy.service); not starting a duplicate."
    echo "To use this script instead: systemctl --user stop ctxgate-proxy.service && $0"
    exit 1
fi

# Load .env with proper quoting (values may contain !, $, spaces).
set -a
. ./.env
set +a
exec nohup setsid python -u proxy/app.py >> proxy.log 2>&1 < /dev/null &
echo "ctxgate-proxy started (detached), pid $!"
