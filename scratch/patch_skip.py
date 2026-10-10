#!/usr/bin/env python3
with open('/home/pawelw/ctxproxy/scratch/run_full_tests.py', 'r') as f:
    lines = f.readlines()

out = []
for line in lines:
    out.append(line)
    if '"CTXGATE_LM_WORKERS": "2",' in line:
        out.append('        "CTXGATE_SKIP_DOTENV": "1",
')

with open('/home/pawelw/ctxproxy/scratch/run_full_tests.py', 'w') as f:
    f.writelines(out)
print("Added CTXGATE_SKIP_DOTENV")

