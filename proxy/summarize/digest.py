"""Session digest building."""



async def _build_session_digest(task_uuid, session_key: str = "", tenant_id: str = "") -> str:
    """Phase 3: Deterministic session digest from ledger + phase summaries."""
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    from proxy.utils.helpers import _clip_at_boundary
    if not pool:
        return ""
    try:
        parts = []
        ledger_rows = await pool.fetch(
            "SELECT kind, title, detail FROM proxy.session_ledger WHERE task_id=$1 AND status='active' ORDER BY id",
            task_uuid
        )
        if ledger_rows:
            ledger_lines = []
            for lr in ledger_rows:
                line = lr["kind"] + ": " + (lr["title"] or "")
                if lr["detail"] and lr["detail"] != lr["title"]:
                    line += " - " + _clip_at_boundary(lr["detail"], 200)
                ledger_lines.append(line)
            if ledger_lines:
                parts.append("LEDGER:" + chr(10) + chr(10).join(ledger_lines))
        phase_rows = await pool.fetch(
            "SELECT phase_number, summary FROM proxy.phase_summaries WHERE task_id=$1 ORDER BY phase_number DESC LIMIT 50",
            task_uuid
        )
        if phase_rows:
            newest = phase_rows[0]
            parts.append("LATEST PHASE (phase " + str(newest["phase_number"]) + "):" + chr(10) + newest["summary"])
            older = []
            for pr in phase_rows[1:]:
                for line in pr["summary"].split(chr(10)):
                    if line.startswith("COMPLETED:") or line.startswith("DECISIONS"):
                        older.append("  " + line.strip())
            if older:
                parts.append("EARLIER PHASES:" + chr(10) + chr(10).join(older[:30]))
        if not parts:
            return ""
        digest = (chr(10) + chr(10) + chr(10)).join(parts)[:6000]
        # Read-only check: skip the INSERT if the newest digest is identical.
        try:
            _prev_digest = await pool.fetchval(
                "SELECT summary FROM proxy.session_summaries "
                "WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1",
                task_uuid,
            )
            if _prev_digest == digest:
                log.debug("Session digest unchanged for task %s — skipping INSERT", task_uuid)
                return digest
        except Exception as e:
            # Never fail the request because of this check.
            log.debug("Digest dedupe check failed (non-fatal): %s", e)
        await pool.execute(
            "INSERT INTO proxy.session_summaries (task_id, session_key, summary, trimmed_msg_count, trimmed_tokens) VALUES ($1,$2,$3,$4,$5)",
            task_uuid, session_key, digest, 0, len(digest) // 4
        )
        log.info("Session digest built: %d chars, %d ledger rows, %d phases", len(digest), len(ledger_rows), len(phase_rows))
        return digest
    except Exception as e:
        log.warning("_build_session_digest failed: %s", e)
        return ""



