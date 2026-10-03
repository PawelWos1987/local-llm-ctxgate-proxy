# Deployment

## Systemd (recommended for 8-hour unattended runs)

1. Copy the repo to `/opt/ctxgate-proxy`:
   sudo mkdir -p /opt/ctxgate-proxy
   sudo rsync -av --delete ./ /opt/ctxgate-proxy/

2. Put your secrets in a file systemd can read:
   sudo mkdir -p /etc/ctxgate-proxy
   sudo tee /etc/ctxgate-proxy/env >/dev/null <<'EOF'
   CTXGATE_DB_DSN=postgresql://...
   CTXGATE_API_KEY=...
   EOF
   sudo chmod 600 /etc/ctxgate-proxy/env

3. Install the units:
   sudo cp deploy/ctxgate-proxy.service /etc/systemd/system/
   sudo cp deploy/ctxgate-worker.service /etc/systemd/system/
   sudo cp deploy/ctxgate-dashboard.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now ctxgate-proxy ctxgate-worker ctxgate-dashboard

4. Verify:
   systemctl is-active ctxgate-proxy    # → active
   systemctl status ctxgate-proxy       # → clean, Restart=always

5. Watch the logs live:
   journalctl -u ctxgate-proxy -f

## Why not `setsid nohup python3 app.py`?

Because `setsid` only detaches from the controlling terminal. It does NOT
move the process out of the session's cgroup. On systemd systems, closing
an SSH session, logging out of a graphical session, or (most commonly)
having an agent's bash tool exit with a timeout sends SIGTERM to every
process in that cgroup — including your `setsid`-detached proxy.

A systemd unit puts the proxy in its own cgroup with its own lifetime,
independent of every shell, session, and tool. That is the only reliable
way to run a service unattended.
