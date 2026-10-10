#!/usr/bin/env python3
path = "/home/pawelw/ctxproxy/scratch/run_tests_v3.py"
with open(path) as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    stripped = line.strip()
    if stripped.startswith("MA ="):
        new_lines.append('MA = "The weather in Paris has been unusually warm this week with temperatures consistently reaching well above the seasonal average of fifteen degrees celsius every single day"\n')
        new_lines.append('MB = "The stock market showed significant and sustained gains across the entire technology sector during this particularly volatile and unpredictable morning trading session on Wall Street today"\n')
        new_lines.append('MC = "The new groundbreaking research paper on quantum computing breakthroughs has been published in the prestigious peer-reviewed journal Nature and received widespread international attention this morning"\n')
        new_lines.append('MD = "The local professional football team dramatically won their championship game last night after an incredibly exciting and heart-stopping final minute of intense play on the field"\n')
        while i < len(lines) and lines[i].strip().startswith(("MA =","MB =","MC =","MD =")):
            i += 1
        continue
    new_lines.append(line)
    i += 1

with open(path, "w") as f:
    f.writelines(new_lines)
print("Updated messages")
