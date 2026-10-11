#!/usr/bin/env python3
"""Patch the calibrate.py to fix the 40-tool syntax error."""
import re

with open("/home/pawelw/ctxproxy/scratch/calibrate.py") as f:
    content = f.read()

# Replace the problematic 40-tool list comprehension with a loop
old_40 = '''        # 40 tools (very large)
        [{"type": "function", "function": {"name": f"enterprise_{i}", "description": f"Enterprise integration tool {i}: manages {['API','data','workflow','deployment','monitoring','security','compliance','analytics'][i%8]} pipelines with multi-stage validation, retry logic, and audit trail requirements", "parameters": {"type": "object", "properties": {f"config_{j}": {"type": "object", "description": f"Configuration block {j} with nested properties for enterprise tool {i}", "properties": {f"nested_{k}": {"type": "string"} for k in range(2)}}, f"simple_{j}": {"type": "string"}} for j in range(3)}, "required": [f"config_0"]}}} for i in range(40)],'''

new_40 = '''        # 40 tools (very large)
        [_make_enterprise_tool(i) for i in range(40)],'''

content = content.replace(old_40, new_40)

# Add the helper function before the tool_sets definition
helper = '''
def _make_enterprise_tool(i):
    domains = ['API','data','workflow','deployment','monitoring','security','compliance','analytics']
    desc = f"Enterprise integration tool {i}: manages {domains[i%8]} pipelines with multi-stage validation, retry logic, and audit trail requirements"
    props = {}
    for j in range(3):
        props[f"config_{j}"] = {
            "type": "object",
            "description": f"Configuration block {j} with nested properties for enterprise tool {i}",
            "properties": {f"nested_{k}": {"type": "string"} for k in range(2)}
        }
        props[f"simple_{j}"] = {"type": "string"}
    return {
        "type": "function",
        "function": {
            "name": f"enterprise_{i}",
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": [f"config_0"]
            }
        }
    }

'''

# Insert helper before "tool_sets = ["
content = content.replace("    tool_sets = [", helper + "    tool_sets = [")

with open("/home/pawelw/ctxproxy/scratch/calibrate.py", "w") as f:
    f.write(content)

print("Patched calibrate.py")

# Verify it parses
import ast
ast.parse(content)
print("AST parse OK")
