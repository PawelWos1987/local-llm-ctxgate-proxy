"""Summarization of trimmed conversation messages."""
import asyncio
import json

_trim_summary_last: dict = {}  # task_uuid -> timestamp of last stored summary


def _msg_text(m) -> str:
    """Extract plain text from a message's content field (handles list-of-parts)."""
    c = m.get("content", "")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _role_capped_msg_text(m) -> str:
    """Role-aware text for the summarizer chunk. Caps tool output
    and assistant tool_call args; leaves user/assistant text alone.
    Never mutates the real message dict."""
    from proxy.config.constants import SUMMARY_TOOL_CAP_CHARS
    role = m.get("role", "")
    t = _msg_text(m)
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


def _build_summarizer_chunks(slice_msgs: list, cap: int) -> list[str]:
    """Build text chunks from messages, respecting per-message cap and SUMMARY_CHUNK_CHARS boundary."""
    from proxy.config.constants import SUMMARY_CHUNK_CHARS, SUMMARY_TOOL_CAP_CHARS
    chunks = []
    cur = []
    cur_len = 0
    for m in slice_msgs:
        t = _role_capped_msg_text(m)
        # A tool message already role-capped to SUMMARY_TOOL_CAP_CHARS is
        # left as-is (the role cap is authoritative for tool output); the
        # generic per-message cap only applies to non-role-capped text.
        _role_capped = (m.get("role") == "tool" and SUMMARY_TOOL_CAP_CHARS > 0
                        and len(_msg_text(m)) > SUMMARY_TOOL_CAP_CHARS)
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
    import time

    from proxy.app import (  # TODO(I4): replace with AppContext
        _slice_log,
        _summary_last_attempt,
        _summary_task_locks,
        log,
        metrics,
        pool,
        session_compactions,
    )
    from proxy.config.constants import SUMMARY_MAX_CHUNKS
    from proxy.ledger.extract import _extract_ledger_entries
    from proxy.ledger.persist import _persist_ledger_entries
    from proxy.lm.call import _call_4b
    from proxy.summarize.slices import _enqueue_context_slices
    from proxy.window.persist import _window_persist
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

            # All text is processed - no skipping. If a slice exceeds max size,
            # it is split into more chunks (handled by _build_summarizer_chunks below).
            # SUMMARY_BACKFILL_CHARS env var retained for backward compat but its
            # skip behavior is removed.

            per_cap = 5000
            chunks = _build_summarizer_chunks(slice_msgs, per_cap)
            if len(chunks) > SUMMARY_MAX_CHUNKS:
                per_cap = 300
                chunks = _build_summarizer_chunks(slice_msgs, per_cap)
            if len(chunks) > SUMMARY_MAX_CHUNKS:
                per_cap = 200
                chunks = _build_summarizer_chunks(slice_msgs, per_cap)
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
                if isinstance(r, BaseException):
                    log.warning("Chunk %d/%d raised: %s", ci + 1, len(chunks), r)
            ok_count = sum(1 for r in chunk_results if r is not None and not isinstance(r, BaseException))
            if ok_count:
                ws["summarized_through"] = end_idx
                try:
                    await _window_persist(session_key, ws)
                except Exception as e:
                    log.warning("Watermark persist failed (non-fatal): %s", e)
                _trim_summary_last[task_uuid] = time.time()
                log.info("Trim summary: %d/%d chunks OK, watermark advanced to %d", ok_count, len(chunks), end_idx)
            else:
                log.warning("Trim summary: all %d chunks failed, watermark unchanged", len(chunks))
        except Exception as e:
            log.warning("_summarize_trimmed_messages failed (will retry next trim): %s", e)
