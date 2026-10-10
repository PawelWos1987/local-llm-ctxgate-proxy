#!/usr/bin/env python3
path = "/home/pawelw/ctxproxy/scratch/run_tests_v3.py"
with open(path) as f:
    content = f.read()

# Add debug print in start() after time.sleep(3)
old = "    time.sleep(3)
    if p.poll() is not None:"
new = "    time.sleep(3)
    _dbg = open(f"{SCRATCH}/sandbox_proxy.log").read()
    for _dl in _dbg.split(chr(10)):
        if "Budget" in _dl:
            print(f"  [DBG] {_dl}")
            break
    else:
        print(f"  [DBG] No Budget line. Log size: {len(_dbg)}")
        for _dl in _dbg.split(chr(10))[:3]:
            if _dl.strip():
                print(f"    {_dl}")
    if p.poll() is not None:"

content = content.replace(old, new)

with open(path, "w") as f:
    f.write(content)
print("Added debug")
