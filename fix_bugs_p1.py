import sys

path = "/home/pawelw/ctxproxy/proxy/app.py"
with open(path, "r") as f:
    lines = f.readlines()

orig_count = len(lines)
changes = []

def find_line(pattern, start=0, end=None):
    if end is None:
        end = len(lines)
    for i in range(start, end):
        if pattern in lines[i]:
            return i
    return -1

def find_line_exact(stripped_pattern, start=0, end=None):
    if end is None:
        end = len(lines)
    for i in range(start, end):
        if lines[i].rstrip() == stripped_pattern:
            return i
    return -1

# ============================================================
# EDIT 1: Add tc_emitted_count = 0 after tc_emitted = False
# ============================================================
i = find_line("tc_emitted = False  # True once we've forwarded")
assert i >= 0, "EDIT 1: tc_emitted = False not found"
indent = "        "
lines.insert(i + 1, indent + "tc_emitted_count = 0  # actual number of tool-call SSE chunks yielded to client\n")
changes.append("EDIT 1: added tc_emitted_count = 0 at line " + str(i + 2))

# Re-find after insert
i = find_line("tc_buffer: list = []  # buffered tool-call chunks")
assert i >= 0, "EDIT 1b: tc_buffer not found"

# ============================================================
# EDIT 2: Main loop flush - add tc_emitted_count += len(tc_buffer)
# Find the main loop's "else:" branch that flushes tc_buffer
# Pattern: "for _tc_chunk in tc_buffer:" followed by "yield" then "tc_buffer.clear()"
# in the MAIN loop (before "if interrupted and not loop_intentional:")
i = find_line("if interrupted and not loop_intentional:")
assert i >= 0, "EDIT 2: 'if interrupted and not loop_intentional' not found"
# The tc_buffer.clear() is a few lines above
# Look backwards for "tc_buffer.clear()"
j = i - 1
while j >= 0 and "tc_buffer.clear()" not in lines[j]:
    j -= 1
assert j >= 0, "EDIT 2: tc_buffer.clear() not found before interrupted check"
# Insert tc_emitted_count += len(tc_buffer) BEFORE tc_buffer.clear()
indent2 = lines[j].split("tc_buffer.clear()")[0]
lines.insert(j, indent2 + "tc_emitted_count += len(tc_buffer)\n")
changes.append("EDIT 2: added tc_emitted_count += len(tc_buffer) at line " + str(j + 1))

# ============================================================
# EDIT 3: Restructure retry inner loop
# Find the retry's "if interrupted:" block (the one with "Retry stream interrupted")
i = find_line('Retry stream interrupted after')
assert i >= 0, "EDIT 3: 'Retry stream interrupted' not found"
# The block is:
#   if interrupted:
#       log.warning(...)
#       exit_reason = "interrupted"
#       finish_reason = "length"
#       break          <-- REMOVE THIS
#       # --- Flush buffered tool calls from retry (after safety check) ---
#       if tc_buffer:
#           ...
#       if loop_in_content:  <-- this is the outer classification

# Find the "break" line right after finish_reason = "length" in the interrupted block
# It should be at i+2 (0-indexed: i is the log.warning line)
# Let me find it precisely
k = i - 1  # go back to "if interrupted:"
while k >= 0 and "if interrupted:" not in lines[k]:
    k -= 1
assert k >= 0, "EDIT 3: 'if interrupted:' not found"

# Now find the break line: it's the line with just "break" after finish_reason = "length"
# within the if interrupted block
brk = k + 1
while brk < len(lines):
    if lines[brk].strip() == "break":
        break
    brk += 1
assert brk < len(lines), "EDIT 3: break line not found"
# Remove the break line
del lines[brk]
changes.append("EDIT 3a: removed unreachable break at line " + str(brk + 1))

# Now the flush block follows. We need to:
# 1. Dedent the flush block by 4 spaces (it was inside the if, now it's at the same level)
# 2. Add the three-way logic (unsafe / complete / incomplete)
# 3. Add "break" AFTER the flush block

// Continue in next chunk
  