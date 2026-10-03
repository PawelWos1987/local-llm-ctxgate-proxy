# Rules for AI agents operating on this machine

## Never touch the proxy lifecycle

The ctxgate-proxy runs under systemd as `ctxgate-proxy.service`. It auto-restarts.

You MUST NOT:
- Run `python proxy/app.py` or `python3 proxy/app.py` in any form.
- Run `pkill`, `kill`, `killall`, `fuser -k`, or `lsof -t | xargs kill` targeting
  the proxy process, its port (9201), or any PID you find via `pgrep -f app.py`.
- Start the proxy as a background job from a bash tool call.
- Use `setsid`, `nohup`, `&`, `disown`, or `screen`/`tmux` to launch the proxy.
- Modify `deploy/*.service` files at runtime.

You MAY:
- Send HTTP requests to `http://127.0.0.1:9201/v1/chat/completions`.
- Read `journalctl -u ctxgate-proxy -n 200` to see proxy logs.
- Call `make ensure-proxy` to verify it is running (this is a read-only check).
- Call `sudo systemctl restart ctxgate-proxy` ONLY if the proxy is genuinely
  unresponsive AND `make ensure-proxy` returns an error.

## Why this rule exists

If you start the proxy from a bash tool, the proxy is a child of your tool's
cgroup. When the tool exits (timeout, session close, cancellation), systemd
sends SIGTERM to the entire cgroup — killing your proxy mid-request. This is
the most common cause of "the proxy stopped working by itself."

Let systemd own the proxy. You own the HTTP client.
