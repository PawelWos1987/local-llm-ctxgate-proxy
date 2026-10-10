#!/usr/bin/env python3
import re

with open('/home/pawelw/ctxproxy/scratch/run_full_tests.py', 'r') as f:
    content = f.read()

# Fix get_log_lines
old = '    r = subprocess.run(f"grep -c \'{pattern}\' {SCRATCH}/sandbox_proxy.log 2>/dev/null || echo 0", shell=True, capture_output=True, text=True)\n    return int(r.stdout.strip() or 0)'
new = '    r = subprocess.run(f"grep -c \'{pattern}\' {SCRATCH}/sandbox_proxy.log 2>/dev/null", shell=True, capture_output=True, text=True)\n    out = r.stdout.strip()\n    return int(out) if out.isdigit() else 0'

if old in content:
    content = content.replace(old, new)
    print("Patched get_log_lines")
else:
    print("Pattern not found, trying alternate")
    # Try a simpler approach - just replace the problematic line
    content = content.replace(
        'r = subprocess.run(f"grep -c \'{pattern}\' {SCRATCH}/sandbox_proxy.log 2>/dev/null || echo 0", shell=True, capture_output=True, text=True)',
        'r = subprocess.run(f"grep -c \'{pattern}\' {SCRATCH}/sandbox_proxy.log 2>/dev/null", shell=True, capture_output=True, text=True)'
    )
    content = content.replace(
        'return int(r.stdout.strip() or 0)',
        'out = r.stdout.strip()\n    return int(out) if out.isdigit() else 0'
    )
    print("Patched via alternate method")

with open('/home/pawelw/ctxproxy/scratch/run_full_tests.py', 'w') as f:
    f.write(content)
print("Done")

