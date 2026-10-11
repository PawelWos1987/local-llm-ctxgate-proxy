#!/usr/bin/env python3
"""Splice new code blocks into proxy/app.py by line range."""
import sys, ast

PATH = "/home/pawelw/ctxproxy/proxy/app.py"
SCRATCH = "/home/pawelw/ctxproxy/scratch"

with open(PATH) as f:
    lines = f.readlines()

def find_func_start(lines, name):
    for i, line in enumerate(lines):
        if line.startswith(f"def {name}(") or line.startswith(f"async def {name}("):
            return i
    return None

def find_func_end(lines, start):
    end = start + 1
    while end < len(lines):
        if lines[end].startswith("def ") or lines[end].startswith("async def "):
            break
        end += 1
    while end > start + 1 and lines[end-1].strip() == "":
        end -= 1
    return end

def read_block(filename):
    with open(f"{SCRATCH}/{filename}") as f:
        return f.read()

# Find all blocks
ct_start = find_func_start(lines, "count_tokens")
smart_start = find_func_start(lines, "_smart_messages_tokens")
smart_end = find_func_end(lines, smart_start)
es_start = find_func_start(lines, "_emergency_shrink")
es_end = find_func_end(lines, es_start)
cc_start = find_func_start(lines, "_check_config")

# Find tokenizer comment in _check_config
tok_comment_idx = None
for i in range(cc_start, min(cc_start + 40, len(lines))):
    if "# Tokenizer" in lines[i]:
        tok_comment_idx = i
        break
tok_block_end = tok_comment_idx + 5

# Find enc loading
enc_start = None
for i in range(len(lines)):
    if "tokenizers.Tokenizer.from_file" in lines[i]:
        enc_start = i - 1
        break
enc_end = enc_start
while enc_end < len(lines):
    if "raise SystemExit(78)" in lines[enc_end] and enc_end > enc_start + 2:
        enc_end += 1
        break
    enc_end += 1

# Find import tokenizers
imp_idx = None
for i in range(len(lines)):
    if lines[i].strip() == "import tokenizers":
        imp_idx = i
        break

print(f"ct_start={ct_start+1} smart_end={smart_end} es_start={es_start+1} es_end={es_end}")
print(f"tok_comment={tok_comment_idx+1} tok_end={tok_block_end} enc_start={enc_start+1} enc_end={enc_end}")
print(f"import at {imp_idx+1 if imp_idx is not None else 'NOT FOUND'}")

# Apply replacements bottom-to-top
# 1. _emergency_shrink
lines[es_start:es_end] = [read_block("new_emergency_shrink.txt")]

# 2. _check_config tokenizer
lines[tok_comment_idx:tok_block_end] = [read_block("new_tok_check.txt")]

# 3. enc loading
lines[enc_start:enc_end] = [read_block("new_enc_load.txt")]

# 4. count_tokens through _smart_messages_tokens
lines[ct_start:smart_end] = [read_block("new_token_funcs.txt")]

# 5. import tokenizers -> optional
if imp_idx is not None:
    lines[imp_idx] = "try:\n    import tokenizers\nexcept ImportError:\n    tokenizers = None\n"

# 6. Add enc = None global (find first "pool" assignment in top 100 lines)
enc_global_idx = None
for i in range(50, 150):
    if "pool" in lines[i] and "=" in lines[i] and not lines[i].startswith("    "):
        enc_global_idx = i
        break
if enc_global_idx is not None:
    lines.insert(enc_global_idx, "enc = None  # tokenizer (only loaded in exact mode)\n")
    print(f"Inserted enc=None at line {enc_global_idx+1}")
else:
    print("WARNING: could not find pool assignment for enc=None insert")

with open(PATH, "w") as f:
    f.writelines(lines)

print(f"Done. New line count: {len(lines)}")

# Verify
with open(PATH) as f:
    src = f.read()
try:
    ast.parse(src)
    print("Syntax: OK")
except SyntaxError as e:
    print(f"Syntax ERROR: line {e.lineno}: {e.msg}")
    sys.exit(1)
