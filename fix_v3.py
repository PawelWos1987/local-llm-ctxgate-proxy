import sys

path = "/home/pawelw/ctxproxy/proxy/app.py"
with open(path, "r") as f:
    lines = f.readlines()

orig_count = len(lines)
changes = []
NL = chr(10)

def find_line(pattern, start=0, end=None):
    if end is None:
        end = len(lines)
    for i in range(start, end):
        if pattern in lines[i]:
            return i
    return -1

# ============================================================
# EDIT 1: Add tc_emitted_count = 0 after tc_emitted = False
# ============================================================
i = find_line("tc_emitted = False  # True once we've forwarded")
assert i >= 0, "EDIT 1: tc_emitted = False not found"
lines.insert(i + 1, "        tc_emitted_count = 0  # actual number of tool-call SSE chunks yielded to client" + NL)
changes.append("EDIT 1: added tc_emitted_count = 0")

# ============================================================
# EDIT 2: Main loop flush - add tc_emitted_count += len(tc_buffer)
# ============================================================
i = find_line("if interrupted and not loop_intentional:")
assert i >= 0, "EDIT 2: interrupted check not found"
j = i - 1
while j >= 0 and "tc_buffer.clear()" not in lines[j]:
    j -= 1
assert j >= 0, "EDIT 2: tc_buffer.clear() not found"
indent2 = lines[j].split("tc_buffer.clear()")[0]
lines.insert(j, indent2 + "tc_emitted_count += len(tc_buffer)" + NL)
changes.append("EDIT 2: added tc_emitted_count increment in main loop")

# ============================================================
# EDIT 3: Restructure retry inner loop
# ============================================================
i = find_line("Retry stream interrupted after")
assert i >= 0, "EDIT 3: Retry stream interrupted not found"
k = i - 1
while k >= 0 and "if interrupted:" not in lines[k]:
    k -= 1
assert k >= 0, "EDIT 3: if interrupted: not found"

brk = k + 1
while brk < len(lines) and lines[brk].strip() != "break":
    brk += 1
assert brk < len(lines), "EDIT 3: break line not found"

end_flush = brk + 1
while end_flush < len(lines) and "if loop_in_content:" not in lines[end_flush]:
    end_flush += 1
assert end_flush < len(lines), "EDIT 3: if loop_in_content: not found"

with open("/home/pawelw/ctxproxy/new_retry_block.txt", "r") as f:
    new_block_lines = f.readlines()

lines[k:end_flush] = new_block_lines
changes.append("EDIT 3: restructured retry flush block")

# ============================================================
# EDIT 4: Guard outer classification
# Find the outer "if loop_in_content:" (at 16-space indent, followed by exit_reason = "loop")
# ============================================================
target = -1
for i in range(len(lines)):
    stripped = lines[i].rstrip()
    if stripped == "                if loop_in_content:" and i + 1 < len(lines):
        if 'exit_reason = "loop"' in lines[i + 1]:
            target = i
            break
assert target >= 0, "EDIT 4: outer if loop_in_content: not found"

indent4 = "                "
# Insert guard lines BEFORE the if, and change if -> elif
guard_line1 = indent4 + 'if exit_reason in ("tool_calls_complete", "unsafe_loop_blocked"):' + NL
guard_line2 = indent4 + "    pass  # retry flush already set the correct exit_reason" + NL
lines[target] = guard_line1 + guard_line2 + indent4 + "elif loop_in_content:" + NL
changes.append("EDIT 4: added guard to prevent clobbering tool_calls_complete")

# ============================================================
# EDIT 5: Fix tc_emitted_n in final block
# ============================================================
i = find_line("tc_emitted_n = tc_accum.count() if tc_complete else 0")
assert i >= 0, "EDIT 5: tc_emitted_n line not found"
indent5 = lines[i].split("tc_emitted_n")[0]
lines[i] = indent5 + "tc_emitted_n = tc_emitted_count" + NL
invariant = indent5 + "if tc_emitted_count == 0:" + NL
invariant += indent5 + "    tc_complete = False" + NL
invariant += indent5 + 'if finish_reason == "tool_calls" and tc_emitted_count == 0:' + NL
invariant += indent5 + '    finish_reason = "stop"' + NL
invariant += indent5 + "    tc_complete = False" + NL
lines.insert(i + 1, invariant)
changes.append("EDIT 5: fixed tc_emitted_n + invariants")

# ============================================================
# EDIT 6: Fix _stream_truncated (both occurrences)
# ============================================================
count = 0
for i in range(len(lines)):
    if '_stream_truncated = (exit_reason != "ok") or loop_in_content or loop_in_reasoning' in lines[i]:
        lines[i] = lines[i].replace(
            '(exit_reason != "ok")',
            '(exit_reason not in ("ok", "tool_calls_complete"))'
        )
        count += 1
assert count >= 1, "EDIT 6: _stream_truncated not found"
changes.append("EDIT 6: fixed _stream_truncated (%d occurrences)" % count)

# ============================================================
# EDIT 7: Add _insert_event_row helper before _enqueue_context_slices
# ============================================================
i = find_line("async def _enqueue_context_slices(")
assert i >= 0, "EDIT 7: _enqueue_context_slices not found"
TQ = chr(34) * 3
helper = (
    "async def _insert_event_row(task_uuid, role, content, meta=None):" + NL +
    "    " + TQ + "Insert one proxy.events row with a race-safe per-task seq." + TQ + NL +
    "    async with pool.acquire() as conn:" + NL +
    "        async with conn.transaction():" + NL +
    "            # Serialize seq allocation per task using the task row as the lock." + NL +
    "            await conn.execute(" + NL +
    '                "SELECT 1 FROM proxy.tasks WHERE id=$1 FOR UPDATE",' + NL +
    "                task_uuid," + NL +
    "            )" + NL +
    "            ns = await conn.fetchval(" + NL +
    '                "SELECT COALESCE(MAX(seq), -1) + 1 FROM proxy.events WHERE task_id=$1",' + NL +
    "                task_uuid," + NL +
    "            )" + NL +
    "            if meta is None:" + NL +
    "                return await conn.fetchval(" + NL +
    '                    "INSERT INTO proxy.events (task_id, seq, role, content) "' + NL +
    '                    "VALUES ($1,$2,$3,$4) RETURNING id",' + NL +
    "                    task_uuid, ns, role, content," + NL +
    "                )" + NL +
    "            return await conn.fetchval(" + NL +
    '                "INSERT INTO proxy.events (task_id, seq, role, content, meta) "' + NL +
    '                "VALUES ($1,$2,$3,$4,$5) RETURNING id",' + NL +
    "                task_uuid, ns, role, content, meta," + NL +
    "            )" + NL +
    NL +
    NL
)
lines.insert(i, helper)
changes.append("EDIT 7: added _insert_event_row helper")

# ============================================================
# EDIT 8: Fix _enqueue_context_slices to use _insert_event_row
# ============================================================
i = find_line("INSERT INTO proxy.events (task_id, role, content, meta) VALUES")
assert i >= 0, "EDIT 8: old INSERT not found"
start = i - 1
while start >= 0 and "event_id = await pool.fetchval(" not in lines[start]:
    start -= 1
assert start >= 0, "EDIT 8: event_id line not found"
end = i + 1
while end < len(lines) and not lines[end].strip().startswith(")"):
    end += 1
assert end < len(lines), "EDIT 8: closing paren not found"
indent8 = lines[start].split("event_id")[0]
new_block = (
    indent8 + "event_id = await _insert_event_row(" + NL +
    indent8 + '    task_uuid, "context_slice", c,' + NL +
    indent8 + '    json.dumps({"slice_start": start_idx, "slice_end": end_idx, "chunk_idx": ci, "dedupe_key": dedupe_key})' + NL +
    indent8 + ")" + NL
)
lines[start:end+1] = [new_block]
changes.append("EDIT 8: replaced _enqueue_context_slices INSERT")

# ============================================================
# EDIT 9: Fix _enqueue_memory_job to use _insert_event_row
# ============================================================
i = find_line("Seq fix: COALESCE(MAX(seq),-1)+1")
assert i >= 0, "EDIT 9: seq fix comment not found"
start = i
end = i + 1
while end < len(lines):
    if "task_uuid, ns, 'user', stored)" in lines[end]:
        end += 1
        break
    end += 1
assert end <= len(lines), "EDIT 9: end of seq block not found"
indent9 = lines[start].split("#")[0]
new_block = (
    indent9 + "# --- Race-safe seq allocation via _insert_event_row ---" + NL +
    indent9 + "ev_id = await _insert_event_row(task_uuid, 'user', stored)" + NL
)
lines[start:end] = [new_block]
changes.append("EDIT 9: replaced _enqueue_memory_job seq+insert")

# ============================================================
# Write the result
# ============================================================
with open(path, "w") as f:
    f.writelines(lines)

print("Original: %d lines" % orig_count)
print("New:      %d lines" % len(lines))
print("Delta:    %+d lines" % (len(lines) - orig_count))
for c in changes:
    print("  " + c)
print("All edits applied successfully.")
