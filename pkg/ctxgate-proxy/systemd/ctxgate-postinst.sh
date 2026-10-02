#!/bin/bash
# ctxgate-proxy post-install: enable and start all services
set -e
echo "=== ctxgate-proxy post-install ==="
if [ ! -f /etc/ctxgate-proxy/.env ]; then
    echo "ERROR: /etc/ctxgate-proxy/.env not found."
    exit 1
fi
install -d /var/log/ctxgate-proxy
systemctl daemon-reload
systemctl enable --now ctxgate-proxy.service
systemctl enable --now ctxgate-worker.service
systemctl enable --now ctxgate-dashboard.service
echo ""
echo "=== Ready ==="
echo "  Proxy:     http://127.0.0.1:9201/v1"
echo "  Dashboard: http://127.0.0.1:9201"
echo "  Logs:      /var/log/ctxgate-proxy/"
echo "  Config:    /etc/ctxgate-proxy/.env"
