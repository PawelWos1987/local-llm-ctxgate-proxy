#!/usr/bin/env python3
"""Apply the retry-path tc_buffer fix to proxy/app.py (from clean dev base)."""
import re

with open("/home/pawelw/ctxproxy/proxy/app.py", "r") as f:
    lines = f.readlines()

# 1. Add BLOCK_UNBOUNDED_LOOPS after RETRY_PRESENCE_PENALTY
for i, line in enumerate(lines):
    if 'RETRY_PRESENCE_PENALTY' in line and '=' in line:
        lines.insert(i + 1, 'BLOCK_UNBOUNDED_LOOPS = os.environ.get("CTXGATE_BLOCK_UNBOUNDED_LOOPS", "1") == "1"\n')
        break

# 2. Add _unbounded_replace_loop() before stream_to_vllm
_unbounded_func = [
    '\n',
    'def _unbounded_replace_loop(code: str) -> bool:\n',
    '    """Detect unbounded while(var.includes(X)) { var = var.replace(X, Y) } loops.\n',
    '    Returns True if the code contains such a pattern without a counter/cap.\n',
    '    """\n',
    '    pattern = re.compile(\n',
    '        r"while\\s*\\(\\s*(\\w+)\\s*\\.\\s*includes\\s*\\([^)]+\\)\\s*\\)\\s*\\{[^}]*?\\1\\s*=\\s*\\1\\s*\\.\\s*replace\\s*\\(",\n',
    '        re.DOTALL,\n',
    '    )\n',
    '    m = pattern.search(code)\n',
    '    if not m:\n',
    '        return False\n',
    '    body_start = code.index("{", m.start())\n',
    '    depth = 0\n',
    '    for pos in range(body_start, len(code)):\n',
    '        if code[pos] == "{":\n',
    '            depth += 1\n',
    '        elif code[pos] == "}":\n',
    '            depth -= 1\n',
    '            if depth == 0:\n',
    '                break\n',
    '    body = code[body_start:pos + 1]\n',
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

# 3. Add tc_buffer = [] after tc_accum in retry setup
retry_log_idx = None
for i, line in enumerate(lines):
    if 'Loop in reasoning - non-thinking retry' in line:
        retry_log_idx = i
        break
if retry_log_idx is None:
    raise RuntimeError("Cannot find 'Loop in reasoning - non-thinking retry'")

tc_accum_idx = None
for i in range(retry_log_idx, retry_log_idx + 30):
    if 'tc_accum = ToolCallAccumulator()' in lines[i]:
        tc_accum_idx = i
        break
if tc_accum_idx is None:
    raise RuntimeError("Cannot find tc_accum in retry path")

indent = '                '
lines.insert(tc_accum_idx + 1, indent + 'tc_buffer = []\n')

# 4. Change retry stream: buffer TCs instead of emitting
tc_yield_idx = None
for i in range(tc_accum_idx, tc_accum_idx + 100):
    if 'tc_accum.add_delta(tool_calls_piece)' in lines[i]:
        for j in range(i, i + 4):
            if 'yield "data: " + json.dumps(tc_chunk)' in lines[j]:
                tc_yield_idx = j
                break
        break
if tc_yield_idx is None:
    raise RuntimeError("Cannot find tc_chunk yield in retry stream")

old_line = lines[tc_yield_idx]
lines[tc_yield_idx] = old_line.replace(
    'yield "data: " + json.dumps(tc_chunk) + "\\n\\n"',
    'tc_buffer.append(tc_chunk)'
)

# 5. THE FIX: Replace unconditional break with flush block + break
standalone_break_idx = None
for i in range(tc_accum_idx, tc_accum_idx + 200):
    stripped = lines[i].strip()
    if stripped == 'break':
        for j in range(i + 1, i + 3):
            if 'if loop_in_content:' in lines[j]:
                standalone_break_idx = i
                break
    if standalone_break_idx is not None:
        break
if standalone_break_idx is None:
    raise RuntimeError("Cannot find standalone break before 'if loop_in_content:'")

# Verify this is the standalone break (not the inner one in if interrupted)
prev_stripped = lines[standalone_break_idx - 1].strip()
if prev_stripped == 'break':
    for i in range(standalone_break_idx + 1, standalone_break_idx + 5):
        if lines[i].strip() == 'break':
            for j in range(i + 1, i + 3):
                if 'if loop_in_content:' in lines[j]:
                    standalone_break_idx = i
                    break
            break

del lines[standalone_break_idx]

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

with open("/home/pawelw/ctxproxy/proxy/app.py", "w") as f:
    f.writelines(lines)

print(f"Done. Total lines: {len(lines)}")
