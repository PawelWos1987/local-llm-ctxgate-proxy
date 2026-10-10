#!/usr/bin/env python3
path = "/home/pawelw/ctxproxy/scratch/run_tests_v2.py"
with open(path) as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    stripped = line.strip()
    if stripped.startswith("MSG_A ="):
        # Replace all MSG lines with proper ones
        new_lines.append('MSG_A = "The quick brown fox jumps over the lazy dog while the cat watches from the tree branch above and the birds sing their morning song in the distant forest clearing"\n')
        new_lines.append('MSG_B = "A large grey elephant walked slowly through the dense tropical jungle carrying a heavy wooden bundle on its back while the river flowed gently beside the ancient stone bridge"\n')
        new_lines.append('MSG_C = "The old lighthouse keeper carefully tended his oil lamp every single evening as the ships passed safely along the rocky coastline and the stars began to appear in the darkening sky"\n')
        new_lines.append('MSG_D = "The young student sat at her desk studying for the upcoming mathematics examination while the rain tapped softly against the window pane outside"\n')
        # Skip all consecutive MSG_ lines
        while i < len(lines) and lines[i].strip().startswith("MSG_"):
            i += 1
        continue
    new_lines.append(line)
    i += 1

with open(path, "w") as f:
    f.writelines(new_lines)
print("Fixed MSG lines")
