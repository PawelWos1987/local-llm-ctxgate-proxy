#!/usr/bin/env python3
path = "/home/pawelw/ctxproxy/scratch/run_tests_v3.py"
with open(path) as f:
    lines = f.readlines()
out = []
for line in lines:
    if "CTXGATE_COMPACTION_NOTE" in line:
        out.append('        "CTXGATE_COMPACTION_NOTE": "",' + chr(10))
    else:
        out.append(line)
with open(path, "w") as f:
    f.writelines(out)
print("fixed")
