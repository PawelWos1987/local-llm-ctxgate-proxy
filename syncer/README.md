# ctxproxy Session Syncer

Independent agent that synchronizes Goose sessions (SQLite) into
ctxproxy tasks (PostgreSQL) with computed status.

## Status Model

| Status     | Condition (updated_at age) |
|------------|---------------------------|
| streaming  | < 5 minutes               |
| idle       | < 2 days                  |
| inactive   | 2 - 7 days                |
| old        | 7 - 30 days               |
| archived   | > 30 days                 |

## Architecture

- **Source**: `~/.local/share/goose/sessions/sessions.db` (SQLite, read-only)
- **Destination**: PostgreSQL `ctxproxy.tasks` table
- **Refresh**: Every 15 minutes
- **Independence**: 100% separated from proxy code. No imports from proxy.

## Installation

```bash
# Copy service file
cp syncer/ctxgate-session-sync.service ~/.config/systemd/user/

# Reload and enable
systemctl --user daemon-reload
systemctl --user enable ctxgate-session-sync.service
systemctl --user start ctxgate-session-sync.service

# Check status
systemctl --user status ctxgate-session-sync.service
```

## Files

- `session_sync.py` - Main syncer script
- `ctxgate-session-sync.service` - systemd user service unit

