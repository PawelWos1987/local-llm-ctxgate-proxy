#!/usr/bin/env python3
path = "/home/pawelw/ctxproxy/scratch/run_tests_v3.py"
with open(path) as f:
    content = f.read()

# Add CTXGATE_COMPACTION_NOTE to the env dict
old = '        "CTXGATE_SKIP_DOTENV": "1",'
new = '        "CTXGATE_SKIP_DOTENV": "1",
        "CTXGATE_COMPACTION_NOTE": "",'
content = content.replace(old, new)

with open(path, "w") as f:
    f.write(content)
print("Added CTXGATE_COMPACTION_NOTE")
