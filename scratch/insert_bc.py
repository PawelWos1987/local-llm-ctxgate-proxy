
import sys

PATH = "/home/pawelw/ctxproxy/proxy/app.py"
BC_FILE = "/home/pawelw/ctxproxy/scratch/build_context_restore.txt"

with open(PATH) as f:
    lines = f.readlines()

with open(BC_FILE) as f:
    bc_content = f.read()

# Insert after line 2935 (0-indexed: 2934)
# Line 2935 is "    return work" (end of _emergency_shrink)
# Line 2936 is "def _track_session_tokens..."
insert_at = 2935  # 0-indexed, so after index 2934

# Add a blank line + the function + a blank line
new_lines = lines[:insert_at] + ["\n"] + bc_content.split("\n") + ["\n"] + lines[insert_at:]

with open(PATH, "w") as f:
    f.writelines(new_lines)

print(f"Inserted build_context at line {insert_at+1}. New total: {len(new_lines)} lines")

import ast
src = "".join(new_lines)
try:
    ast.parse(src)
    print("Syntax: OK")
except SyntaxError as e:
    print(f"Syntax ERROR: line {e.lineno}: {e.msg}")
    sys.exit(1)
