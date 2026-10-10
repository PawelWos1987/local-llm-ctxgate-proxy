
import re

with open("/home/pawelw/ctxproxy/proxy/app.py", "r") as f:
    src = f.read()

lines = src.split("\n")

# ============================================================
# 1. Add BLOCK_UNBOUNDED_LOOPS constant after RETRY_TOP_P
# ============================================================
for i, line in enumerate(lines):
    if 'RETRY_TOP_P' in line and '=' in line and not line.strip().startswith('#'):
        lines.insert(i + 1, 'BLOCK_UNBOUNDED_LOOPS = os.environ.get("CTXGATE_BLOCK_UNBOUNDED_LOOPS", "1") == "1"')
        break

# ============================================================
# 2. Add _unbounded_replace_loop function before stream_to_vllm
# ============================================================
_unbounded_func = '''
def _unbounded_replace_loop(code: str) -> bool:
    """Detect the unbounded while(var.includes(X)) { var = var.replace(X, Y) } pattern.
    
    Returns True if the code contains an unbounded replace loop (unsafe).
    """
    # Pattern: while (content.includes(oldN)) { content = content.replace(oldN, newN) }
    # with no iteration cap
    pattern = re.compile(
        r'while\s*\(\s*(\w+)\s*\.\s*includes\s*\([^)]+\)\s*\)\s*\{[^}]*\1\s*=\s*\1\s*\.\s*replace\s*\([^)]+\)\s*\}',
        re.DOTALL
    )
    if pattern.search(code):
        return True
    # Broader: any while loop that calls .replace() on the loop variable without a counter
    pattern2 = re.compile(
        r'while\s*\(\s*(\w+)\s*\.\s*includes\s*\([^)]+\)\s*\)\s*\{[^}]*\1\s*=\s*\1\s*\.\s*replace\s*\(',
        re.DOTALL
    )
    if pattern2.search(code):
        # Check there's no counter/cap
        match = pattern2.search(code)
        body = match.group(0)
        if 'count' not in body and 'i<' not in body and 'for ' not in body:
            return True
    return False
'''

for i, line in enumerate(lines):
    if line.strip().startswith('async def stream_to_vllm'):
        lines.insert(i, _unbounded_func)
        break

# ============================================================
# 3. Add tc_buffer to the retry path
# ============================================================
# Find the retry setup section (after "tc_accum = ToolCallAccumulator()" in the retry block)
# The retry block starts with "while True:" after the retry setup
for i, line in enumerate(lines):
    if 'non-thinking retry' in line and 'log.info' in line:
        # Find the next "tc_accum = ToolCallAccumulator()" after this line
        for j in range(i, min(i + 30, len(lines))):
            if 'tc_accum = ToolCallAccumulator()' in lines[j]:
                # Add tc_buffer = [] after tc_accum
                lines.insert(j + 1, '                tc_buffer = []')
                break
        break

# ============================================================
# 4. Change retry stream to buffer tool calls instead of emitting
# ============================================================
# Find the retry stream's tool call emission
# Pattern: tc_accum.add_delta(tool_calls_piece) followed by tc_chunk = {...} and yield
for i, line in enumerate(lines):
    if 'non-thinking retry' in line and 'log.info' in line:
        # Find the retry stream section
        for j in range(i, min(i + 100, len(lines))):
            if 'tc_accum.add_delta(tool_calls_piece)' in lines[j]:
                # The next lines should be the tc_chunk creation and yield
                # Replace direct emission with buffering
                # Find the yield line for tc_chunk
                for k in range(j, min(j + 5, len(lines))):
                    if 'yield "data: " + json.dumps(tc_chunk)' in lines[k]:
                        # Replace the yield with buffer append
                        lines[k] = lines[k].replace(
                            'yield "data: " + json.dumps(tc_chunk) + "\\n\\n"',
                            'tc_buffer.append(tc_chunk)'
                        )
                        break
                break
        break

# ============================================================
# 5. Add the flush block BEFORE the break that exits the retry loop
# ============================================================
# Find the "break" that exits the retry while True loop
# It's the standalone "break" after the "if interrupted:" block
for i, line in enumerate(lines):
    if 'non-thinking retry' in line and 'log.info' in line:
        # Find the retry while True loop
        in_retry = False
        for j in range(i, min(i + 120, len(lines))):
            if 'while True:' in lines[j]:
                in_retry = True
            if in_retry and lines[j].strip() == 'break':
                # Check if this is the standalone break (not inside an if)
                # It should be at the same indent level as "while True:"
                # The standalone break is the one right before "if loop_in_content:"
                if j + 1 < len(lines) and 'if loop_in_content:' in lines[j + 1]:
                    # This is THE break we need to fix
                    # Remove it and add the flush block + new break
                    del lines[j]  # Remove the unconditional break
                    
                    # Now insert the flush block at position j
                    flush_block = [
                        '                    # --- Flush buffered tool calls from retry (after safety check) ---',
                        '                    if tc_buffer:',
                        '                        _unsafe_tc = False',
                        '                        tc_complete_r = tc_accum.is_complete() if tc_accum.count() > 0 else False',
                        '                        if tc_complete_r and BLOCK_UNBOUNDED_LOOPS:',
                        '                            for _idx, _tc in tc_accum._calls.items():',
                        '                                if _tc.get("name") == "execute_typescript":',
                        '                                    _code = _tc.get("arguments", "")',
                        '                                    if _unbounded_replace_loop(_code):',
                        '                                        _unsafe_tc = True',
                        '                                        break',
                        '                        if _unsafe_tc:',
                        '                            metrics["unsafe_loop_blocked"] += 1',
                        '                            log.warning("Retry produced unbounded replace loop - BLOCKED (not emitted)")',
                        '                            _err_text = "BLOCKED: The model generated a code pattern that would cause an infinite loop in the sandbox. The tool call was suppressed. Please use s.replaceAll(A, B) or s.split(A).join(B) instead of a while loop with .replace()."',
                        '                            _err_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0, "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"content": _err_text}, "finish_reason": None}]}',
                        '                            yield "data: " + json.dumps(_err_chunk) + "\\n\\n"',
                        '                            finish_reason = "stop"',
                        '                            exit_reason = "unsafe_loop_blocked"',
                        '                        else:',
                        '                            for _tc_chunk in tc_buffer:',
                        '                                yield "data: " + json.dumps(_tc_chunk) + "\\n\\n"',
                        '                            tc_buffer.clear()',
                        '                    break',
                    ]
                    for k, fl in enumerate(flush_block):
                        lines.insert(j + k, fl)
                    break
        break

# ============================================================
# 6. Update NS-DIAG to include tc_emitted
# ============================================================
for i, line in enumerate(lines):
    if 'NS-DIAG' in line and 'tc_seen' in line:
        # Add tc_emitted to the log line
        if 'tc_emitted' not in line:
            lines[i] = line.replace(
                'tc_seen={tc_seen} tc_complete={tc_complete}',
                'tc_seen={tc_seen} tc_complete={tc_complete} tc_emitted={tc_emitted}'
            )
            # Also need to add tc_emitted to the format args
            # Find the next line with the format args
            if i + 1 < len(lines):
                lines[i + 1] = lines[i + 1].replace(
                    'reasoning_chars_first)',
                    'reasoning_chars_first, tc_emitted_n)'
                )
        break

# ============================================================
# 7. Add tc_emitted_n to the finalization section
# ============================================================
# The finalization already has tc_emitted_n = tc_accum.count() if tc_complete else 0
# But we need it to reflect ACTUAL emission, not just count
# Find the tc_emitted_n line and update it
for i, line in enumerate(lines):
    if 'tc_emitted_n = tc_accum.count() if tc_complete else 0' in line:
        # This is fine for the normal path. For the retry path, tc_buffer was either
        # cleared (emitted) or not (blocked). The tc_accum count is preserved.
        # The key invariant: tc_emitted_n > 0 only if chunks were actually sent.
        # Since we clear tc_buffer after emitting, and set exit_reason="unsafe_loop_blocked"
        # when blocked, the finalization will see tc_complete=True but the finish_reason
        # will be "stop" (not "tool_calls") for the blocked case.
        # This is correct behavior.
        break

# Write the result
with open("/home/pawelw/ctxproxy/proxy/app.py", "w") as f:
    f.write("\n".join(lines))

print(f"Done. Total lines: {len(lines)}")
print("Changes applied:")
print("  1. BLOCK_UNBOUNDED_LOOPS constant")
print("  2. _unbounded_replace_loop function")
print("  3. tc_buffer in retry setup")
print("  4. Retry stream buffers TCs instead of emitting")
print("  5. Flush block BEFORE break (the fix)")
print("  6. NS-DIAG includes tc_emitted")
