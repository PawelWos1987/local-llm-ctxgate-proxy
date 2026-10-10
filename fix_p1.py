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
