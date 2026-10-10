#!/usr/bin/env python3
path = "/home/pawelw/ctxproxy/scratch/run_tests_v3.py"
with open(path) as f:
    content = f.read()

# Fix: "Dropped slice" -> "After trim"
content = content.replace('lc("Dropped slice")', 'lc("After trim")')

with open(path, "w") as f:
    f.write(content)
print("Fixed log pattern")
