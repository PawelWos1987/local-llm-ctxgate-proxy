#!/usr/bin/env python3
"""
ctxproxy session syncer
=======================
Independent agent that synchronizes Goose sessions (SQLite) into
ctxproxy proxy.tasks (PostgreSQL).

Source:  ~/.local/share/goose/sessions/sessions.db (table: sessions)
Dest:    postgresql://...@127.0.0.1:5432/ctxproxy (schema: proxy, table: tasks)

Mapping:
  sessions.id         -> proxy.tasks.session_id
  sessions.name       -> proxy.tasks.name
  sessions.created_at -> proxy.tasks.created_at (TIMESTAMPTZ)
  sessions.updated_at -> proxy.tasks.updated_at (TIMESTAMPTZ)
  computed status     -> proxy.tasks.status
  now (on change)     -> proxy.tasks.status_last_update_date

Also: for rows in proxy.tasks NOT present in sessions.db (proxy-created),
      status is recomputed from their own updated_at.

Status model (based on updated_at age):
  - active_streaming: < 15 minutes
  - active:           < 60 minutes
  - idle:             < 2 days
  - inactive:         2 - 7 days
  - old:              7 - 30 days
  - archived:         > 30 days

Runs every 15 minutes via systemd user service.
100% independent from proxy code.
"""

import sqlite3
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

# --- Configuration ---
GOOSE_DB = os.path.expanduser("~/.local/share/goose/sessions/sessions.db")
PG_DSN = os.environ.get(
    "CTXPROXY_PG_DSN",
    "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy",
)
REFRESH_SECONDS = 15 * 60  # 15 minutes
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "syncer.log")

# Status thresholds
STREAMING_THRESHOLD = timedelta(minutes=15)
ACTIVE_THRESHOLD = timedelta(minutes=60)
IDLE_THRESHOLD = timedelta(days=2)
INACTIVE_THRESHOLD = timedelta(days=7)
OLD_THRESHOLD = timedelta(days=30)

# Fixed namespace for deterministic UUID generation
UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("session-syncer")


def deterministic_uuid(session_id: str) -> str:
    """Generate a stable UUID from session_id (same input = same UUID)."""
    return str(uuid.uuid5(UUID_NAMESPACE, session_id))


def parse_ts_to_utc(val: str) -> datetime:
    """Parse naive local timestamp from SQLite to aware UTC datetime."""
    dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
    local_dt = dt.astimezone()
    return local_dt.astimezone(timezone.utc)


def compute_status(updated_at: datetime, now_utc: datetime) -> str:
    """Compute task status based on how old updated_at is.

    - active_streaming: < 15 min
    - active:           < 60 min
    - idle:             < 2 days
    - inactive:         2-7 days
    - old:              7-30 days
    - archived:         > 30 days
    """
    age = now_utc - updated_at
    if age < timedelta(0) or age < STREAMING_THRESHOLD:
        return "active_streaming"
    elif age < ACTIVE_THRESHOLD:
        return "active"
    elif age < IDLE_THRESHOLD:
        return "idle"
    elif age < INACTIVE_THRESHOLD:
        return "inactive"
    elif age < OLD_THRESHOLD:
        return "old"
    else:
        return "archived"


def read_sessions() -> list[dict]:
    """Read all sessions from Goose SQLite database."""
    conn = sqlite3.connect(GOOSE_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, name, description, working_dir, created_at, updated_at, "
            "total_tokens, session_type, provider_name FROM sessions"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def sync():
    """One sync pass:
    1. Upsert all sessions from SQLite into proxy.tasks (name, dates, status)
    2. For proxy-only rows (not in sessions.db), recompute status from updated_at
    """
    import asyncpg

    sessions = read_sessions()
    now_utc = datetime.now(timezone.utc)
    log.info("Read %d sessions from Goose DB", len(sessions))

    conn = await asyncpg.connect(PG_DSN)
    try:
        upserted = 0
        status_counts: dict[str, int] = {}
        session_ids_in_source = set()

        # --- Part 1: Upsert sessions from SQLite ---
        for s in sessions:
            session_id = s["id"]
            session_ids_in_source.add(session_id)
            task_uuid = deterministic_uuid(session_id)
            created = parse_ts_to_utc(s["created_at"])
            updated = parse_ts_to_utc(s["updated_at"])
            status = compute_status(updated, now_utc)
            status_counts[status] = status_counts.get(status, 0) + 1

            await conn.execute(
                """
                INSERT INTO proxy.tasks (id, session_id, name,
                                         created_at, updated_at, status,
                                         session_type, working_dir, provider_name,
                                         status_last_update_date)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    status = EXCLUDED.status,
                    session_type = EXCLUDED.session_type,
                    working_dir = EXCLUDED.working_dir,
                    provider_name = EXCLUDED.provider_name,
                    status_last_update_date = CASE
                        WHEN proxy.tasks.status != EXCLUDED.status THEN NOW()
                        ELSE proxy.tasks.status_last_update_date
                    END
                """,
                uuid.UUID(task_uuid),
                session_id,
                s["name"],
                created,
                updated,
                status,
                s["session_type"],
                s["working_dir"],
                s["provider_name"],
            )
            upserted += 1

        # --- Part 2: Recompute status for proxy-only rows (not in sessions.db) ---
        proxy_only = await conn.fetch(
            """
            SELECT session_id, updated_at, status FROM proxy.tasks
            WHERE session_id NOT IN (SELECT unnest($1::text[]))
            """,
            list(session_ids_in_source),
        )

        updated_proxy_only = 0
        for row in proxy_only:
            new_status = compute_status(row["updated_at"], now_utc)
            if new_status != row["status"]:
                await conn.execute(
                    """
                    UPDATE proxy.tasks
                    SET status = $1, status_last_update_date = NOW()
                    WHERE session_id = $2
                    """,
                    new_status,
                    row["session_id"],
                )
                updated_proxy_only += 1
            status_counts[new_status] = status_counts.get(new_status, 0) + 1

        log.info(
            "Synced %d sessions, %d proxy-only rows updated. Status: %s",
            upserted,
            updated_proxy_only,
            status_counts,
        )
    finally:
        await conn.close()


async def main():
    """Main loop: sync every REFRESH_SECONDS."""
    log.info("Session syncer starting (refresh=%ds, target=proxy.tasks)", REFRESH_SECONDS)
    while True:
        try:
            await sync()
        except Exception as e:
            log.error("Sync failed: %s", e, exc_info=True)
        await asyncio.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Session syncer stopped")

