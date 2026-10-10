
import sys

with open("/home/pawelw/ctxproxy/proxy/app.py", "r") as f:
    lines = f.readlines()

# Find the unconditional break at line 4930 (0-indexed: 4929)
# It's the line "                    break" that comes right after the "if interrupted:" block
# and before "# --- Flush buffered tool calls from retry (after safety check) ---"

# Verify we're at the right place
idx = 4929  # 0-indexed for line 4930
assert lines[idx].strip() == "break", f"Expected 'break' at line 4930, got: {lines[idx].strip()}"
assert "Flush buffered tool calls from retry" in lines[idx+1], f"Expected flush comment at line 4931, got: {lines[idx+1].strip()}"

# Remove the unconditional break (line 4930)
del lines[idx]

# Now find the "tc_buffer.clear()" that ends the flush block
# After deletion, the flush block's tc_buffer.clear() is at the line after the for loop
# It should be the line "                            tc_buffer.clear()" 
# Find it: it's the last line of the "else:" block in the flush section
# After removing line 4930, the old line 4958 is now at index 4956
tc_clear_idx = None
for i in range(idx, idx + 30):
    if lines[i].strip() == "tc_buffer.clear()" and "    " * 28 in lines[i]:
        tc_clear_idx = i
        break

if tc_clear_idx is None:
    # Try finding it more broadly
    for i in range(idx, idx + 40):
        if lines[i].strip() == "tc_buffer.clear()":
            tc_clear_idx = i
            break

assert tc_clear_idx is not None, "Could not find tc_buffer.clear() in flush block"
assert lines[tc_clear_idx].strip() == "tc_buffer.clear()", f"Unexpected: {lines[tc_clear_idx]}"

# Insert a break after tc_buffer.clear()
lines.insert(tc_clear_idx + 1, "                    break\n")

with open("/home/pawelw/ctxproxy/proxy/app.py", "w") as f:
    f.writelines(lines)

print(f"Done. Removed break at line 4930, added break after tc_buffer.clear() at line {tc_clear_idx+2}")
print(f"Total lines: {len(lines)}")
