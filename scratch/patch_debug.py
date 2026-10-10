#!/usr/bin/env python3
path = "/home/pawelw/ctxproxy/scratch/run_tests_v2.py"
with open(path) as f:
    content = f.read()

# Add debug print after time.sleep(3) in start_proxy
old = """    time.sleep(3)
    if proc.poll() is not None:
        log = open(f"{SCRATCH}/sandbox_proxy.log").read()
        raise RuntimeError(f"Proxy died: {log[-300:]}")
    return proc"""

new = """    time.sleep(3)
    if proc.poll() is not None:
        log = open(f"{SCRATCH}/sandbox_proxy.log").read()
        raise RuntimeError(f"Proxy died: {log[-300:]}")
    # Debug: print budget config from log
    _log = open(f"{SCRATCH}/sandbox_proxy.log").read()
    for _l in _log.split("\n"):
        if "Budget config" in _l:
            print(f"  [DEBUG] {_l}")
            break
    else:
        print(f"  [DEBUG] No Budget config line found! First 3 lines:")
        for _l in _log.split("\n")[:3]:
            if _l.strip():
                print(f"    {_l}")
    return proc"""

content = content.replace(old, new)

with open(path, "w") as f:
    f.write(content)
print("Added debug to start_proxy")
