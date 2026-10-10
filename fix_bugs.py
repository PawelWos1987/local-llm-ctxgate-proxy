import re, sys

path = "/home/pawelw/ctxproxy/proxy/app.py"
with open(path, "r") as f:
    src = f.read()

original = src

# ============================================================
# EDIT 1: Add tc_emitted_count = 0 in generate() scope
# ============================================================
old1 = "        tc_emitted = False  # True once we've forwarded a complete tool-call set\n        tc_buffer: list = []  # buffered tool-call chunks (flushed after safety check)"
new1 = "        tc_emitted = False  # True once we've forwarded a complete tool-call set\n        tc_emitted_count = 0  # actual number of tool-call SSE chunks yielded to client\n        tc_buffer: list = []  # buffered tool-call chunks (flushed after safety check)"
assert old1 in src, "EDIT 1 anchor not found"
src = src.replace(old1, new1, 1)

# ============================================================
# EDIT 2: Main loop flush - track tc_emitted_count
# ============================================================
old2 = "                    else:\n                        for _tc_chunk in tc_buffer:\n                            yield \"data: \" + json.dumps(_tc_chunk) + \"\\n\\n\"\n                        tc_buffer.clear()\n\n                if interrupted and not loop_intentional:"
new2 = "                    else:\n                        for _tc_chunk in tc_buffer:\n                            yield \"data: \" + json.dumps(_tc_chunk) + \"\\n\\n\"\n                        tc_emitted_count += len(tc_buffer)\n                        tc_buffer.clear()\n\n                if interrupted and not loop_intentional:"
assert old2 in src, "EDIT 2 anchor not found"
src = src.replace(old2, new2, 1)

# ============================================================
# EDIT 3: Restructure retry inner loop
# ============================================================
old3 = """                if interrupted:
                    log.warning("Retry stream interrupted after %d chars: %s", len(full_content), interrupted)
                    exit_reason = "interrupted"
                    finish_reason = "length"
                    break
                    # --- Flush buffered tool calls from retry (after safety check) ---
                    if tc_buffer:
                        _unsafe_tc = False
                        tc_complete_r = tc_accum.is_complete() if tc_accum.count() > 0 else False
                        if tc_complete_r and BLOCK_UNBOUNDED_LOOPS:
                            for _idx, _tc in tc_accum._calls.items():
                                if _tc.get("name") == "execute_typescript":
                                    try:
                                        _args = json.loads(_tc.get("arguments", "{}"))
                                    except Exception:
                                        _args = {}
                                    if _unbounded_replace_loop(_args.get("code", "")):
                                        _unsafe_tc = True
                                        break
                        if _unsafe_tc:
                            metrics["unsafe_loop_blocked"] += 1
                            log.warning("Stream retry: blocked unbounded replace-loop (session=%s)", session_key)
                            tc_buffer.clear()
                            tc_accum = ToolCallAccumulator()
                            _err_text = "BLOCKED: execute_typescript code contains an unbounded while(var.includes(X)) { var = var.replace(X, Y) } loop. Use s.replaceAll(A, B) or s.split(A).join(B) instead. Any loop must have an explicit iteration cap."
                            _err_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0, "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"content": _err_text}, "finish_reason": None}]}
                            yield "data: " + json.dumps(_err_chunk) + "\n\n"
                            finish_reason = "stop"
                            exit_reason = "unsafe_loop_blocked"
                        else:
                            for _tc_chunk in tc_buffer:
                                yield "data: " + json.dumps(_tc_chunk) + "\n\n"
                            tc_buffer.clear()
                if loop_in_content:"""

new3 = """                if interrupted:
                    log.warning("Retry stream interrupted after %d chars: %s", len(full_content), interrupted)
                    exit_reason = "interrupted"
                    finish_reason = "length"
                # --- Flush buffered tool calls from retry (after safety check) ---
                if tc_buffer:
                    _unsafe_tc = False
                    tc_complete_r = tc_accum.is_complete() if tc_accum.count() > 0 else False
                    if tc_complete_r and BLOCK_UNBOUNDED_LOOPS:
                        for _idx, _tc in tc_accum._calls.items():
                            if _tc.get("name") == "execute_typescript":
                                try:
                                    _args = json.loads(_tc.get("arguments", "{}"))
                                except Exception:
                                    _args = {}
                                if _unbounded_replace_loop(_args.get("code", "")):
                                    _unsafe_tc = True
                                    break
                    if _unsafe_tc:
                        metrics["unsafe_loop_blocked"] += 1
                        log.warning("Stream retry: blocked unbounded replace-loop (session=%s)", session_key)
                        tc_buffer.clear()
                        tc_accum = ToolCallAccumulator()
                        _err_text = "BLOCKED: execute_typescript code contains an unbounded while(var.includes(X)) { var = var.replace(X, Y) } loop. Use s.replaceAll(A, B) or s.split(A).join(B) instead. Any loop must have an explicit iteration cap."
                        _err_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0, "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"content": _err_text}, "finish_reason": None}]}
                        yield "data: " + json.dumps(_err_chunk) + "\n\n"
                        finish_reason = "stop"
                        exit_reason = "unsafe_loop_blocked"
                    elif tc_complete_r:
                        for _tc_chunk in tc_buffer:
                            yield "data: " + json.dumps(_tc_chunk) + "\n\n"
                        tc_emitted_count += len(tc_buffer)
                        tc_buffer.clear()
                        finish_reason = "tool_calls"
                        exit_reason = "tool_calls_complete"
                    else:
                        # Incomplete: do not emit partial tool-call chunks
                        tc_buffer.clear()
                        finish_reason = "length"
                break
                if loop_in_content:"""

assert old3 in src, "EDIT 3 anchor not found"
src = src.replace(old3, new3, 1)

# ============================================================
# EDIT 4: Guard outer classification
# ============================================================
old4 = """                if loop_in_content:
                    exit_reason = "loop"
                    finish_reason = "length"
                elif loop_in_reasoning or reasoning_overflow:
                    exit_reason = "reasoning_overflow"
                    finish_reason = "length"
                elif finish_reason == "length":
                    # O3: retry segment hit max_tokens without completing
                    exit_reason = "retry_length"
                else:
                    exit_reason = "loop_recovered"

            # Classify empty responses"""

new4 = """                if exit_reason in ("tool_calls_complete", "unsafe_loop_blocked"):
                    pass  # retry flush already set the correct exit_reason
                elif loop_in_content:
                    exit_reason = "loop"
                    finish_reason = "length"
                elif loop_in_reasoning or reasoning_overflow:
                    exit_reason = "reasoning_overflow"
                    finish_reason = "length"
                elif finish_reason == "length":
                    # O3: retry segment hit max_tokens without completing
                    exit_reason = "retry_length"
                else:
                    exit_reason = "loop_recovered"

            # Classify empty responses"""

assert old4 in src, "EDIT 4 anchor not found"
src = src.replace(old4, new4, 1)

# ============================================================
# EDIT 5: Fix tc_emitted_n in final block
# ============================================================
old5 = """            tc_complete = tc_accum.is_complete() if tc_accum.count() > 0 else False
            tc_truncated = tc_accum.count() > 0 and not tc_complete
            tc_emitted_n = tc_accum.count() if tc_complete else 0
            if tc_truncated:"""

new5 = """            tc_complete = tc_accum.is_complete() if tc_accum.count() > 0 else False
            tc_truncated = tc_accum.count() > 0 and not tc_complete
            tc_emitted_n = tc_emitted_count
            if tc_emitted_count == 0:
                tc_complete = False
            if finish_reason == "tool_calls" and tc_emitted_count == 0:
                finish_reason = "stop"
                tc_complete = False
            if tc_truncated:"""

assert old5 in src, "EDIT 5 anchor not found"
src = src.replace(old5, new5, 1)

# ============================================================
# EDIT 6: Fix _stream_truncated
# ============================================================
old6 = '            _stream_truncated = (exit_reason != "ok") or loop_in_content or loop_in_reasoning\n            # One concise diagnostic line'
new6 = '            _stream_truncated = (exit_reason not in ("ok", "tool_calls_complete")) or loop_in_content or loop_in_reasoning\n            # One concise diagnostic line'
assert old6 in src, "EDIT 6 anchor not found"
src = src.replace(old6, new6, 1)

old6b = '            _stream_truncated = (exit_reason != "ok") or loop_in_content or loop_in_reasoning\n            for _cg in _emit_ctxgate_final'
new6b = '            _stream_truncated = (exit_reason not in ("ok", "tool_calls_complete")) or loop_in_content or loop_in_reasoning\n            for _cg in _emit_ctxgate_final'
if old6b in src:
    src = src.replace(old6b, new6b, 1)

# ============================================================
# EDIT 7: Add _insert_event_row helper
# ============================================================
old7 = "async def _enqueue_context_slices(task_uuid, session_key, chunks, start_idx, end_idx):"
new7 = 'async def _insert_event_row(task_uuid, role, content, meta=None):\n    """Insert one proxy.events row with a race-safe per-task seq."""\n    async with pool.acquire() as conn:\n        async with conn.transaction():\n            # Serialize seq allocation per task using the task row as the lock.\n            await conn.execute(\n                "SELECT 1 FROM proxy.tasks WHERE id=$1 FOR UPDATE",\n                task_uuid,\n            )\n            ns = await conn.fetchval(\n                "SELECT COALESCE(MAX(seq), -1) + 1 FROM proxy.events WHERE task_id=$1",\n                task_uuid,\n            )\n            if meta is None:\n                return await conn.fetchval(\n                    "INSERT INTO proxy.events (task_id, seq, role, content) "\n                    "VALUES ($1,$2,$3,$4) RETURNING id",\n                    task_uuid, ns, role, content,\n                )\n            return await conn.fetchval(\n                "INSERT INTO proxy.events (task_id, seq, role, content, meta) "\n                "VALUES ($1,$2,$3,$4,$5) RETURNING id",\n                task_uuid, ns, role, content, meta,\n            )\n\n' + old7
assert old7 in src, "EDIT 7 anchor not found"
src = src.replace(old7, new7, 1)

# ============================================================
# EDIT 8: Fix _enqueue_context_slices INSERT
# ============================================================
old8 = '''            event_id = await pool.fetchval(
                "INSERT INTO proxy.events (task_id, role, content, meta) VALUES ($1,$2,$3,$4) RETURNING id",
                task_uuid, "context_slice", c,
                json.dumps({"slice_start": start_idx, "slice_end": end_idx, "chunk_idx": ci, "dedupe_key": dedupe_key})
            )'''

new8 = '''            event_id = await _insert_event_row(
                task_uuid, "context_slice", c,
                json.dumps({"slice_start": start_idx, "slice_end": end_idx, "chunk_idx": ci, "dedupe_key": dedupe_key})
            )'''

assert old8 in src, "EDIT 8 anchor not found"
src = src.replace(old8, new8, 1)

# ============================================================
# EDIT 9: Fix _enqueue_memory_job
# ============================================================
old9 = '''        # --- Seq fix: COALESCE(MAX(seq),-1)+1 ---
        seq_row = await pool.fetchrow(
            'SELECT COALESCE(MAX(seq),-1) + 1 AS ns FROM proxy.events WHERE task_id=$1', task_uuid)
        ns = seq_row['ns'] if seq_row else 0
        ev_id = await pool.fetchval(
            'INSERT INTO proxy.events (task_id, seq, role, content) VALUES ($1,$2,$3,$4) RETURNING id',
            task_uuid, ns, 'user', stored)'''

new9 = '''        # --- Race-safe seq allocation via _insert_event_row ---
        ev_id = await _insert_event_row(task_uuid, 'user', stored)'''

assert old9 in src, "EDIT 9 anchor not found"
src = src.replace(old9, new9, 1)

# ============================================================
# Write the result
# ============================================================
with open(path, "w") as f:
    f.write(src)

lines_orig = original.split("\n")
lines_new = src.split("\n")
print(f"Original: {len(lines_orig)} lines")
print(f"New:      {len(lines_new)} lines")
print(f"Delta:    {len(lines_new) - len(lines_orig)} lines")
print("All 9 edits applied successfully.")
