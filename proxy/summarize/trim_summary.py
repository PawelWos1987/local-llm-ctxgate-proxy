"""Summarization of trimmed conversation messages."""
import hashlib
import asyncio
import json

_trim_summary_last: dict = {}  # task_uuid -> timestamp of last stored summary



async def _summarize_trimmed_messages(task_uuid, session_key, rest, session_name: str = "", pending_cut: int = None):
    """Exactly-once, restart-safe summarization of the dropped region.

    The slice is ALWAYS [summarized_through, dropped_total) re-derived from the
    current in-memory window state (Goose resends the full history, so rest is
    always complete). Both bounds are monotonic, so the tiling has no gaps and no
    overlaps: every dropped message is summarized exactly once, in order.
    On success the watermark advances to dropped_total and is persisted; on
    failure it is left unchanged (WARNING) so the slice is re-derived and retried
    on a later request. A brand-new / pre-change session (no watermark) backfills
    only the NEWEST SUMMARY_BACKFILL_CHARS of the dropped region, in order.
    """
    from proxy.app import log, metrics, pool, session_compactions, _summary_task_locks, _summary_last_attempt, _slice_log  # TODO(I4): replace with AppContext
    from proxy.config.constants import RHO_COLD, SUMMARY_CHUNK_CHARS, SUMMARY_MAX_CHUNKS, SUMMARY_TOOL_CAP_CHARS
    from proxy.ledger.extract import _extract_ledger_entries
    from proxy.ledger.persist import _persist_ledger_entries
    from proxy.lm.call import _call_4b
    from proxy.summarize.digest import _build_session_digest
    from proxy.summarize.slices import _enqueue_context_slices
    from proxy.window.persist import _window_persist
    import time
    global _summary_last_attempt
    if not pool or not rest:
        return
    lock = _summary_task_locks.get(task_uuid)
    if lock is None:
        lock = asyncio.Lock()
        _summary_task_locks[task_uuid] = lock
    async with lock:
        ws = session_compactions.get(session_key)
        if ws is None:
            return
        try:
            start_idx = ws.get("summarized_through", 0)
            end_idx = ws.get("dropped_total", ws.get("cut", 0))
            if pending_cut is not None:
                end_idx = min(end_idx, pending_cut)
            _slice_log.append((start_idx, end_idx))
            if len(_slice_log) > 1000:
                _slice_log[:] = _slice_log[-500:]
            _summary_last_attempt[task_uuid] = (time.time(), end_idx)
            if end_idx <= start_idx:
                return  # nothing new to summarize
            slice_msgs = rest[start_idx:end_idx]
            if not slice_msgs:
                return

            def _mtext(m):
                c = m.get("content", "")
                if isinstance(c, list):
                    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                return c or ""

            def _role_capped_text(m):
                """Role-aware text for the summarizer chunk. Caps tool output
                and assistant tool_call args; leaves user/assistant text alone.
                Never mutates the real message dict."""
                role = m.get("role", "")
                t = _mtext(m)
                if role == "tool" and SUMMARY_TOOL_CAP_CHARS > 0 and len(t) > SUMMARY_TOOL_CAP_CHARS:
                    omitted = len(t) - SUMMARY_TOOL_CAP_CHARS
                    t = t[:int(SUMMARY_TOOL_CAP_CHARS * 0.6)] + "[..." + str(omitted) + " chars omitted...]" + t[-int(SUMMARY_TOOL_CAP_CHARS * 0.4):]
                if role == "assistant":
                    tcs = m.get("tool_calls")
                    if tcs:
                        parts = []
                        for tc in tcs:
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            if isinstance(args, dict):
                                args = json.dumps(args)
                            if len(args) > 300:
                                args = args[:300] + "..."
                            parts.append("[tool_call:" + name + "(" + args + ")]")
                        if parts:
                            t = t + " " + " ".join(parts)
                return t

            # All text is processed - no skipping. If a slice exceeds max size,
            # it is split into more chunks (handled by _build_chunks below).
            # SUMMARY_BACKFILL_CHARS env var retained for backward compat but its
            # skip behavior is removed.

            per_cap = 5000
            def _build_chunks(cap):
                chunks = []
                cur = []
                cur_len = 0
                for m in slice_msgs:
                    t = _role_capped_text(m)
                    # A tool message already role-capped to SUMMARY_TOOL_CAP_CHARS is
                    # left as-is (the role cap is authoritative for tool output); the
                    # generic per-message cap only applies to non-role-capped text.
                    _role_capped = (m.get("role") == "tool" and SUMMARY_TOOL_CAP_CHARS > 0
                                    and len(_mtext(m)) > SUMMARY_TOOL_CAP_CHARS)
                    # BUG 3: keep head (60%) + tail (40%) of the per-message cap
                    # so tool outputs' results (usually at the tail) are not lost.
                    if not _role_capped and len(t) > cap:
                        _head = int(cap * 0.6)
                        _tail = cap - _head
                        t = t[:_head] + " ...[truncated]... " + t[-_tail:]
                    line = str(m.get("role", "?")) + ": " + t
                    if not line.strip():
                        continue
                    if cur and cur_len + len(line) + 1 > SUMMARY_CHUNK_CHARS:
                        chunks.append("\n".join(cur))
                        cur = []
                        cur_len = 0
                    cur.append(line)
                    cur_len += len(line) + 1
                if cur:
                    chunks.append("\n".join(cur))
                return chunks
            chunks = _build_chunks(per_cap)
            if len(chunks) > SUMMARY_MAX_CHUNKS:
                per_cap = 300
                chunks = _build_chunks(per_cap)
            if len(chunks) > SUMMARY_MAX_CHUNKS:
                per_cap = 200
                chunks = _build_chunks(per_cap)
            if not chunks:
                return
            # Phase 5: Enqueue context slices for the worker
            try:
                await _enqueue_context_slices(task_uuid, session_key, chunks, start_idx, end_idx)
            except Exception as e:
                log.warning('Context slice enqueue failed (non-fatal): %s', e)

            last_phase_row = await pool.fetchrow("SELECT MAX(phase_number) as mp FROM proxy.phase_summaries WHERE task_id=$1", task_uuid)
            last_phase = (last_phase_row["mp"] or 0) if last_phase_row else 0
            # BUG 4: the phase prompt now requires explicit, labeled task-state
            # fields so the model retains task progress across a trim.
            phase_prompt = ("You are a conversation phase summarizer. Read the new messages and produce a concise summary of the task state after this phase. "
                           "Write it in PLAIN TEXT (no markdown) with EXACTLY these labeled fields, in this order: "
                           "'CURRENT TASK/PHASE: ' (what the agent is working on now); "
                           "'COMPLETED: ' (what is done, WITH evidence - name the reports/files and test results that prove it); "
                           "'IN PROGRESS: ' (what is actively being worked); "
                           "'NEXT STEP: ' (the single concrete next action); "
                           "'DECISIONS/CONSTRAINTS: ' (decisions made and constraints that must hold); "
                           "'DO NOT REDO: ' (work already finished that must not be repeated). "
                           "'ARTIFACTS: ' (every file created or modified in this phase, one line each with its purpose); "
                           "'VERIFICATION: ' (test results, build outcomes, or other verification seen in this phase); "
                           "Be precise and factual. No commentary. At most 500 words.")
            async def _do_chunk(ci: int, chunk: str):
                new_phase = last_phase + 1 + ci
                session_tag = (" [Session: " + session_name + "]") if session_name else ""
                phase_user_msg = "Phase " + str(new_phase) + session_tag + ":\n" + chunk
                phase_result = await _call_4b(
                    [{"role": "user", "content": phase_user_msg}],
                    max_tokens=800, json_mode=False, system=phase_prompt,
                    kind="phase",
                )
                phase_summary = ""
                if isinstance(phase_result, str) and phase_result.strip():
                    phase_summary = phase_result.strip()
                elif isinstance(phase_result, dict):
                    su = phase_result.get("state_update", {})
                    if isinstance(su, dict) and su.get("current_state"):
                        phase_summary = su["current_state"].strip()
                if not phase_summary:
                    log.warning("Phase %d summarization: no usable text (chunk %d/%d)", new_phase, ci + 1, len(chunks))
                    return None
                await pool.execute(
                    "INSERT INTO proxy.phase_summaries (task_id, session_key, phase_number, summary, trimmed_msg_count, trimmed_tokens) "
                    "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (task_id, phase_number) DO UPDATE SET summary=$4, trimmed_msg_count=$5, trimmed_tokens=$6",
                    task_uuid, session_key, new_phase, phase_summary[:3000], len(slice_msgs), len(chunk),
                )
                metrics["summary_slices_ok"] += 1
                log.info("Phase %d summary stored: %d msgs, %d tokens, %d chars", new_phase, len(slice_msgs), len(chunk), len(phase_summary))
                return phase_summary

            # Phase 3: Run deterministic extractor on the full-fidelity slice
            try:
                ledger_entries = _extract_ledger_entries(rest[start_idx:end_idx], start_idx, end_idx, task_uuid)
                if ledger_entries:
                    n_inserted = await _persist_ledger_entries(ledger_entries, task_uuid)
                    log.info('Ledger: %d entries, %d inserted (start=%d end=%d)', len(ledger_entries), n_inserted, start_idx, end_idx)
            except Exception as e:
                log.warning('Ledger extraction failed (non-fatal): %s', e)

            chunk_results = await asyncio.gather(
                *[_do_chunk(ci, chunk) for ci, chunk in enumerate(chunks)],
                return_exceptions=True,
            )
            for ci, r in enumerate(chunk_results):
                if isinstance(r, Exception):
                    log.warning("Phase chunk %d/%d failed: %s", ci + 1, len(chunks), r)
            # Per-chunk state tracking: only advance watermark when ALL chunks succeed
            all_succeeded = True
            for ci, r in enumerate(chunk_results):
                if isinstance(r, Exception):
                    log.warning("Phase chunk %d/%d failed: %s", ci + 1, len(chunks), r)
                    all_succeeded = False
                    try:
                        _cid = hashlib.sha256(f"{task_uuid}:{start_idx}:{end_idx}:{ci}".encode()).hexdigest()
                        await pool.execute(
                            "INSERT INTO proxy.summary_chunks (task_id, session_key, chunk_id, start_idx, end_idx, chunk_seq, status) "
                            "VALUES ($1,$2,$3,$4,$5,$6,'failed') "
                            "ON CONFLICT (task_id, chunk_id) DO UPDATE SET status='failed', updated_at=now()",
                            task_uuid, session_key, _cid, start_idx, end_idx, ci
                        )
                    except Exception:
                        pass
                elif isinstance(r, str) and r:
                    try:
                        _cid = hashlib.sha256(f"{task_uuid}:{start_idx}:{end_idx}:{ci}".encode()).hexdigest()
                        await pool.execute(
                            "INSERT INTO proxy.summary_chunks (task_id, session_key, chunk_id, start_idx, end_idx, chunk_seq, status, summary) "
                            "VALUES ($1,$2,$3,$4,$5,$6,'succeeded',$7) "
                            "ON CONFLICT (task_id, chunk_id) DO UPDATE SET status='succeeded', summary=$7, updated_at=now()",
                            task_uuid, session_key, _cid, start_idx, end_idx, ci, r
                        )
                    except Exception:
                        pass
                else:
                    all_succeeded = False
            if not all_succeeded:
                metrics["summary_slices_failed"] += 1
                log.warning("Slice summarization: not all chunks succeeded (start=%d end=%d) - watermark NOT advanced past failed chunks", start_idx, end_idx)
                return

            # Phase 3: Replace LLM root step with deterministic digest
            try:
                digest = await _build_session_digest(task_uuid, session_key)
                if digest:
                    _trim_summary_last[task_uuid] = time.time()
                    log.info('Session digest stored: %d chars', len(digest))
            except Exception as e:
                log.warning('Session digest build failed (non-fatal): %s', e)

            if end_idx > ws.get("summarized_through", 0):
                ws["summarized_through"] = end_idx
                await _window_persist(session_key, ws)
                log.info("Watermark advanced: session=%s summarized_through=%d", session_key, end_idx)
        except Exception as e:
            metrics["summary_slices_failed"] += 1
            log.warning("Slice summarization failed (watermark NOT advanced, will retry): %s", e)
        finally:
            # BUG 2: always release the in-flight flag on EVERY exit path
            # (early returns, no-phase, exception) so future triggers are not
            # blocked. Previously early returns left in_flight=True forever.
            ws["in_flight"] = False




