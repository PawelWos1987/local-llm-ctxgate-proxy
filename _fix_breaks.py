#!/usr/bin/env python3
with open("/home/pawelw/ctxproxy/proxy/app.py", "r") as f:
    lines = f.readlines()

# Find the "if interrupted:" block in the retry path
# It should be followed by the flush block
for i, line in enumerate(lines):
    if 'if interrupted:' in line and i > 4600:  # In the retry path
        # The next lines should be:
        #   log.warning(...)
        #   exit_reason = "interrupted"
        #   finish_reason = "length"
        #   [break is MISSING here]
        #   # --- Flush buffered tool calls ---
        # Find "finish_reason = "length"" after "if interrupted:"
        for j in range(i, i + 10):
            if 'finish_reason = "length"' in lines[j] and 'interrupted' in lines[i]:
                # Check if next line is the flush comment (meaning break is missing)
                if j + 1 < len(lines) and 'Flush buffered tool calls' in lines[j + 1]:
                    # Insert break after finish_reason = "length"
                    indent = lines[j][:len(lines[j]) - len(lines[j].lstrip())]
                    lines.insert(j + 1, indent + 'break\n')
                    print(f"Added break after 'finish_reason = length' at line {j+2}")
                    break
        break

# Now find and remove the duplicate break
# After the fix above, we should have:
#   ...
#   break          <- from if interrupted (just added)
#   # --- Flush ---
#   if tc_buffer:
#       ...
#   break          <- after flush (correct)
#   break          <- DUPLICATE
#   if loop_in_content:
#
# Find two consecutive breaks where the second is followed by "if loop_in_content:"
for i in range(len(lines) - 2):
    if lines[i].strip() == 'break' and lines[i+1].strip() == 'break':
        # Check if the line after the second break is "if loop_in_content:"
        if i + 2 < len(lines) and 'if loop_in_content:' in lines[i+2]:
            # Remove the second break (the duplicate)
            del lines[i+1]
            print(f"Removed duplicate break at line {i+2}")
            break

with open("/home/pawelw/ctxproxy/proxy/app.py", "w") as f:
    f.writelines(lines)

print(f"Total lines: {len(lines)}")
