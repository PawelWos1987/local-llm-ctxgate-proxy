#!/bin/bash
# ctxgate-proxy Flatpak entrypoint
# Starts the proxy, worker, and dashboard, then opens the GUI (browser)

set +H
export APP_DIR="/app/ctxgate"
cd "$APP_DIR"

# Load environment
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

# Default ports (override via .env)
export CTXGATE_PROXY_PORT=${CTXGATE_PROXY_PORT:-9201}
export CTXGATE_DASHBOARD_PORT=${CTXGATE_DASHBOARD_PORT:-9202}

echo "[ctxgate] Starting services..."

# Start the proxy (with supervisor for auto-restart)
bash supervisor.sh &
SUPERVISOR_PID=$!

# Start the worker
python3 -u worker/worker.py >> worker.log 2>&1 &
WORKER_PID=$!

# Start the dashboard
python3 -u dashboard/dashboard.py >> dashboard.log 2>&1 &
DASHBOARD_PID=$!

echo "[ctxgate] Proxy supervisor PID: $SUPERVISOR_PID"
echo "[ctxgate] Worker PID: $WORKER_PID"
echo "[ctxgate] Dashboard PID: $DASHBOARD_PID"

# Wait for services to be ready
sleep 3

# Open the GUI (dashboard in browser)
DASHBOARD_URL="http://127.0.0.1:$CTXGATE_DASHBOARD_PORT"
echo "[ctxgate] Dashboard available at: $DASHBOARD_URL"

# Try to open in browser (X11/Wayland)
if command -v xdg-open &> /dev/null; then
    xdg-open "$DASHBOARD_URL" &
elif command -v sensible-browser &> /dev/null; then
    sensible-browser "$DASHBOARD_URL" &
elif command -v firefox &> /dev/null; then
    firefox "$DASHBOARD_URL" &
elif command -v chromium &> /dev/null; then
    chromium "$DASHBOARD_URL" &
fi

# Keep the app alive (wait for any child to exit)
echo "[ctxgate] Running. Press Ctrl+C to stop all services."
trap "kill $SUPERVISOR_PID $WORKER_PID $DASHBOARD_PID 2>/dev/null; exit 0" SIGINT SIGTERM
wait
