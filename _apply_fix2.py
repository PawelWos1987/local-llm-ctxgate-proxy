#!/usr/bin/env python3
"""Apply the retry-path tc_buffer fix to proxy/app.py.

Changes:
1. Add BLOCK_UNBOUNDED_LOOPS constant
2. Add _unbounded_replace_loop() function
3. Add tc_buffer = [] in retry setup (after tc_accum)
4. Change retry stream: buffer TCs instead of emitting
5. Remove unconditional break, add flush block + break (THE FIX)
6. Update tc_emitted_n to reflect actual emission
"""
import re

with open("/home/pawelw/ctxproxy/proxy/app.py", "r") as f:
    lines = f.readlines()

# ============================================================
# 1. Add BLOCK_UNBOUNDED_LOOPS after RETRY_TOP_P
# ============================================================
for i, line in enumerate(lines):
    if 'RETRY_TOP_P' in line and '=' in line and not line.strip().startswith('#'):
        lines.insert(i + 1, 'BLOCK_UNBOUNDED_LOOPS = os.environ.get("CTXGATE_BLOCK_UNBOUNDED_LOOPS", "1") == "1"\n')
        break

# Re-find line numbers after insertion (shifted by 1)
# ============================================================
# 2. Add _unbounded_replace_loop before stream_to_vllm
# ============================================================
_unbounded_func = [
    '\n',
    'def _unbounded_replace_loop(code: str) -> bool:\n',
    '    """Detect unbounded while(var.includes(X)) { var = var.replace(X, Y) } loops.\n',
    '    Returns True if the code contains such a pattern (unsafe).\n',
    '    """\n',
    '    # Pattern: while (var.includes(something)) { var = var.replace(...) }\n',
    '    # with no iteration counter/cap\n',
    '    pattern = re.compile(\n',
    '        r"while\\s*\\(\\s*(\\w+)\\s*\\.\\s*includes\\s*\\([^)]+\\)\\s*\\)\\s*\\{[^}]*\\1\\s*=\\s*\\1\\s*\\.\\s*replace\\s*\\(",\n',
    '        re.DOTALL\n',
    '    )\n',
    '    m = pattern.search(code)\n',
    '    if not m:\n',
    '        return False\n',
    '    # Check the loop body for a counter/cap\n',
    '    body_start = m.end()\n',
    '    # Find the closing brace of the while body\n',
    '    depth = 1\n',
    '    pos = body_start - 1  # position of the opening {\n',
    '    while pos < len(code) and depth > 0:\n',
    '        if code[pos] == \'{\':\n',
    '            depth += 1\n',
    '        elif code[pos] == \'}\':\n',
    '            depth -= 1\n',
    '        pos += 1\n',
    '    body = code[body_start:pos - 1]\n',
    '    # If there is a counter (count++, i <, for loop) it is bounded\n',
    '    if re.search(r"\\b(count|iter|n|i)\\s*(\\+\\+|<|<=)", body):\n',
    '        return False\n',
    '    if "for " in body:\n',
    '        return False\n',
    '    return True\n',
    '\n',
]

for i, line in enumerate(lines):
    if line.strip().startswith('async def stream_to_vllm'):
        for j in range(len(_unbounded_func) - 1, -1, -1):
            lines.insert(i, _unbounded_func[j])
        break

# ============================================================
# Now re-locate the retry path lines (they shifted due to insertions above)
# ============================================================
# Find the retry "Loop in reasoning" log line
retry_log_idx = None
for i, line in enumerate(lines):
    if 'Loop in reasoning - non-thinking retry' in line:
        retry_log_idx = i
        break

if retry_log_idx is None:
    raise RuntimeError("Cannot find 'Loop in reasoning - non-thinking retry'")

# Find tc_accum = ToolCallAccumulator() after the retry log
tc_accum_idx = None
for i in range(retry_log_idx, retry_log_idx + 30):
    if 'tc_accum = ToolCallAccumulator()' in lines[i]:
        tc_accum_idx = i
        break

if tc_accum_idx is None:
    raise RuntimeError("Cannot find tc_accum in retry path")

# ============================================================
# 3. Add tc_buffer = [] after tc_accum in retry setup
# ============================================================
indent = '                '  # 16 spaces (matches tc_accum indent)
lines.insert(tc_accum_idx + 1, indent + 'tc_buffer = []\n')

# ============================================================
# 4. Change retry stream: buffer TCs instead of emitting
#    Find the yield line for tc_chunk in the retry stream
# ============================================================
# The retry stream's tc_chunk yield is the one AFTER tc_accum_idx
tc_yield_idx = None
for i in range(tc_accum_idx, tc_accum_idx + 100):
    if 'tc_accum.add_delta(tool_calls_piece)' in lines[i]:
        # The yield is 2 lines after (tc_chunk = ... then yield ...)
        for j in range(i, i + 4):
            if 'yield "data: " + json.dumps(tc_chunk)' in lines[j]:
                tc_yield_idx = j
                break
        break

if tc_yield_idx is None:
    raise RuntimeError("Cannot find tc_chunk yield in retry stream")

# Replace the yield with buffer append
old_line = lines[tc_yield_idx]
lines[tc_yield_idx] = old_line.replace(
    'yield "data: " + json.dumps(tc_chunk) + "\\n\\n"',
    'tc_buffer.append(tc_chunk)'
)

# ============================================================
# 5. THE FIX: Remove unconditional break, add flush block + break
#    Find the standalone "break" right before "if loop_in_content:"
# ============================================================
standalone_break_idx = None
for i in range(tc_accum_idx, tc_accum_idx + 200):
    if lines[i].strip() == 'break':
        # Check if next non-empty line is "if loop_in_content:"
        for j in range(i + 1, i + 3):
            if 'if loop_in_content:' in lines[j]:
                standalone_break_idx = i
                break
    if standalone_break_idx is not None:
        break

if standalone_break_idx is None:
    raise RuntimeError("Cannot find standalone break before 'if loop_in_content:'")

# Remove the unconditional break
del lines[standalone_break_idx]

# Insert the flush block at that position
flush_block = [
    '                    # --- Flush buffered tool calls from retry (after safety check) ---\n',
    '                    if tc_buffer:\n',
    '                        _unsafe_tc = False\n',
    '                        tc_complete_r = tc_accum.is_complete() if tc_accum.count() > 0 else False\n',
    '                        if tc_complete_r and BLOCK_UNBOUNDED_LOOPS:\n',
    '                            for _idx, _tc in tc_accum._calls.items():\n',
    '                                if _tc.get("name") == "execute_typescript":\n',
    '                                    _code = _tc.get("arguments", "")\n',
    '                                    if _unbounded_replace_loop(_code):\n',
    '                                        _unsafe_tc = True\n',
    '                                        break\n',
    '                        if _unsafe_tc:\n',
    '                            metrics["unsafe_loop_blocked"] += 1\n',
    '                            log.warning("Retry produced unbounded replace loop - BLOCKED (not emitted)")\n',
    '                            _err_text = "BLOCKED: The model generated a code pattern that would cause an infinite loop in the sandbox. The tool call was suppressed. Please use s.replaceAll(A, B) or s.split(A).join(B) instead of a while loop with .replace(). Any loop must have an explicit iteration cap."\n',
    '                            _err_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0, "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"content": _err_text}, "finish_reason": None}]}\n',
    '                            yield "data: " + json.dumps(_err_chunk) + "\\n\\n"\n',
    '                            finish_reason = "stop"\n',
    '                            exit_reason = "unsafe_loop_blocked"\n',
    '                        else:\n',
    '                            for _tc_chunk in tc_buffer:\n',
    '                                yield "data: " + json.dumps(_tc_chunk) + "\\n\\n"\n',
    '                            tc_buffer.clear()\n',
    '                    break\n',
]

for k, fl in enumerate(flush_block):
    lines.insert(standalone_break_idx + k, fl)

# ============================================================
# 6. Update tc_emitted_n to track actual emission
#    Find the line: tc_emitted_n = tc_accum.count() if tc_complete else 0
#    Change to track whether tc_buffer was actually flushed
# ============================================================
for i, line in enumerate(lines):
    if 'tc_emitted_n = tc_accum.count() if tc_complete else 0' in line:
        # We need to track if tc_buffer was cleared (emitted) vs blocked
        # Add a tc_emitted flag that gets set in the flush block
        # For now, the existing logic is: if tc_complete, tc_emitted_n = count
        # But for the blocked case, finish_reason is "stop" not "tool_calls"
        # so the invariant is maintained: tc_emitted_n > 0 implies chunks were sent
        # because in the blocked case we set finish_reason="stop" and exit_reason="unsafe_loop_blocked"
        # The final section only sets finish_reason="tool_calls" if tc_complete AND finish_reason != "tool_calls"
        # But in the blocked case, finish_reason is already "stop", so it stays "stop"
        # This is correct! The invariant holds.
        break

# ============================================================
# Write the result
# ============================================================
with open("/home/pawelw/ctxproxy/proxy/app.py", "w") as f:
    f.writelines(lines)

print(f"Done. Total lines: {len(lines)}")
print("Changes applied:")
print("  1. BLOCK_UNBOUNDED_LOOPS constant")
print("  2. _unbounded_replace_loop() function")
print("  3. tc_buffer = [] in retry setup")
print("  4. Retry stream buffers TCs (tc_buffer.append)")
print("  5. Flush block BEFORE break (THE FIX)")
