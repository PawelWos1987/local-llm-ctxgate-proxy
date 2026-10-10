#!/usr/bin/env python3
import re

path = "/home/pawelw/ctxproxy/scratch/run_full_tests.py"
with open(path) as f:
    content = f.read()

# Find and replace the start_proxy function's env construction
# The current code does: env = {**os.environ} then env.update({...})
# We need: env = {minimal dict}

old_start = "    env = {**os.environ}"
new_start = "    env = {\n        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),\n        "HOME": os.environ.get("HOME", "/home/pawelw"),\n        "LANG": os.environ.get("LANG", "en_US.UTF-8"),\n    }"

if old_start in content:
    content = content.replace(old_start, new_start)
    print("Replaced env construction with minimal env")
else:
    print("WARNING: could not find 'env = {**os.environ}'")
    # Try alternate
    if "env = {**os.environ}" in content:
        content = content.replace("env = {**os.environ}", new_start)
        print("Replaced via alternate")
    else:
        print("COULD NOT PATCH - pattern not found")

with open(path, "w") as f:
    f.write(content)
print("Done")

