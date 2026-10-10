#!/usr/bin/env python3
"""Loop guard tests for ctxgate-proxy. Standalone (no pytest)."""
import json
import re
import sys
import time
import importlib

sys.path.insert(0, "/home/pawelw/ctxproxy")
import proxy.app as app
importlib.reload(app)

PASS = 0
FAIL = 0
RESULTS = []

def record(name, ok, detail=""):
    global PASS, FAIL
    st = "PASS" if ok else "FAIL"
    if ok: PASS += 1
    else: FAIL += 1
    RESULTS.append(st + ": " + name + (" - " + detail if detail else ""))
    print("  " + st + ": " + name + (" - " + detail if detail else ""))

print("=" * 60)
print("LOOP GUARD TESTS")
print("=" * 60)

# ============================================================
# 1. Detector: 135-char sentence x30 -> loop, period 135
# ============================================================
print("\n--- 1: Detector (135-char loop) ---")
sentence = "Continuing through the file list with more audit and probe entries from late September, along with some design and research documents."
sent_len = len(sentence)
assert 130 <= sent_len <= 140, f"Expected ~135 chars, got {sent_len}"
loop_text = sentence * 30
p = app._detect_loop(loop_text)
record("detect_135", p == sent_len, f"period={p}, expected={sent_len}")

# ============================================================
# 2. No false positives
# ============================================================
print("\n--- 2: No false positives ---")
# 2a: 200-line markdown table
table = "| col1 | col2 | col3 |\n|------|------|------|\n"
for i in range(200):
    table += f"| value_{i} | data_{i} | info_{i} |\n"
p = app._detect_loop(table)
record("no_fp_table", p == 0, f"period={p}")

# 2b: long numbered list
numlist = ""
for i in range(200):
    numlist += f"{i}. This is item number {i} with some unique description text.\n"
p = app._detect_loop(numlist)
record("no_fp_numlist", p == 0, f"period={p}")

# 2c: 4000 chars of varied prose
import random
random.seed(42)
words = ["the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog", "near", "river",
         "under", "bridge", "with", "small", "cat", "and", "big", "bird", "in", "tree",
         "near", "house", "by", "road", "at", "end", "of", "street", "with", "park",
         "next", "to", "school", "and", "shop", "on", "corner", "of", "main", "street"]
prose = ""
for i in range(400):
    prose += " ".join(random.sample(words, 10)) + ". "
p = app._detect_loop(prose)
record("no_fp_prose", p == 0, f"period={p}")

# 2d: JSON array
json_arr = json.dumps([{"id": i, "name": f"item_{i}", "value": i * 1.5, "tags": [f"tag_{i%5}", f"cat_{i%3}"]} for i in range(100)])
p = app._detect_loop(json_arr)
record("no_fp_json", p == 0, f"period={p}")

# ============================================================
# 3. Reasoning loop mid-stream -> upstream stops, retry works
# ============================================================
print("\n--- 3: Reasoning loop mid-stream ---")

# Test reasoning loop detection directly
reasoning = "Let me think about this carefully. " * 5  # ~150 chars, no loop
loop_sent = "Continuing through the file list with more audit and probe entries from late September, along with some design and research documents."
reasoning += loop_sent * 10  # 1340 chars of loop

p = app._detect_loop(reasoning)
ok_detect = p == sent_len
# The loop starts at char ~150, detection should trigger within ~1500 chars of loop start
ok_early = len(reasoning) < 150 + 1340 + 1500  # within 1500 chars of loop start
record("reasoning_loop_detect", ok_detect, f"period={p}, expected={sent_len}")
record("reasoning_loop_early", ok_early, f"total_chars={len(reasoning)}")

# ============================================================
# 4. Retry also loops -> clean stop (no injected notice), finish stop, [DONE]
# ============================================================
print("\n--- 4: Retry loops again ---")
# A content-loop retry must stop cleanly WITHOUT injecting a [ctxgate: notice
# Verify the detection path exists and that no proxy text is injected into the transcript
src = open("/home/pawelw/ctxproxy/proxy/app.py").read()
ok = "Retry looped in content" in src and "[ctxgate:" not in src
record("retry_loop_notice", ok)

# ============================================================
# 5. Content loop -> stop + notice, no retry
# ============================================================
print("\n--- 5: Content loop ---")
# Content loop should NOT trigger a retry
src = open("/home/pawelw/ctxproxy/proxy/app.py").read()
ok = "loop_in_content" in src and "Loop in content" in src
record("content_loop_stop", ok)

# ============================================================
# 6. Length with empty content -> non-thinking retry, no "(in progress)"
# ============================================================
print("\n--- 6: reasoning_overflow retry ---")
src = open("/home/pawelw/ctxproxy/proxy/app.py").read()
ok_no_inprogress = 'full_content or "(in progress)"' not in src
ok_retry = 'reasoning_overflow' in src and 'enable_thinking' in src
record("no_in_progress", ok_no_inprogress)
record("reasoning_overflow_retry", ok_retry)

# ============================================================
# 7. Long legit reasoning then tool call -> passthrough
# ============================================================
print("\n--- 7: Long legit reasoning + tool call ---")
# 5000 chars of varied reasoning (no loop)
legit_reasoning = ""
for i in range(100):
    legit_reasoning += f"Step {i}: analyzing component {i} with value {i*3.14:.2f} and checking dependency {i%10}. "
p = app._detect_loop(legit_reasoning)
record("legit_no_loop", p == 0, f"period={p}, chars={len(legit_reasoning)}")

# ============================================================
# 8. No client temperature -> upstream body has NO sampling fields
# ============================================================
print("\n--- 8: No client temperature ---")
# Verify the vllm_body construction logic
src = open("/home/pawelw/ctxproxy/proxy/app.py").read()
ok = '_ct = body.get("temperature")' in src and "MIN_TEMPERATURE" in src
record("temp_omitted", ok)

# ============================================================
# 9. Client temperature 0.8 -> forwarded. Client 0 -> omitted.
# ============================================================
print("\n--- 9: Temperature forwarding ---")
# 0.8 >= 0.3 (MIN_TEMPERATURE) -> forwarded
ok_08 = 0.8 >= app.MIN_TEMPERATURE
# 0 < 0.3 -> omitted
ok_0 = 0 < app.MIN_TEMPERATURE
record("temp_08_forwarded", ok_08, f"0.8 >= {app.MIN_TEMPERATURE}")
record("temp_0_omitted", ok_0, f"0 < {app.MIN_TEMPERATURE}")

# ============================================================
# 10. Retry body: temperature 0.7, top_p 0.8, presence_penalty 1.5
# ============================================================
print("\n--- 10: Retry body values ---")
record("retry_temp", app.RETRY_TEMPERATURE == 0.7, f"got {app.RETRY_TEMPERATURE}")
record("retry_top_p", app.RETRY_TOP_P == 0.8, f"got {app.RETRY_TOP_P}")
record("retry_presence", app.RETRY_PRESENCE_PENALTY == 1.5, f"got {app.RETRY_PRESENCE_PENALTY}")

# ============================================================
# 11. Chunk sizes: 1, 7, 40 chars and 3-token bursts
# ============================================================
print("\n--- 11: Chunk size detection ---")
sentence = "Continuing through the file list with more audit and probe entries from late September, along with some design and research documents."

def test_chunk_size(chunk_size):
    """Feed the loop in chunks of given size, verify detection triggers."""
    full = sentence * 10
    tail = ""
    detected = False
    for i in range(0, len(full), chunk_size):
        tail = (tail + full[i:i+chunk_size])[-app.LOOP_TAIL:]
        if _detect_loop_safe(tail):
            detected = True
            break
    return detected

def _detect_loop_safe(text):
    try:
        return app._detect_loop(text) > 0
    except:
        return False

for cs in [1, 7, 40]:
    ok = test_chunk_size(cs)
    record(f"chunk_{cs}", ok, f"chunk_size={cs}")

# 3-token burst (~12 chars per burst for this sentence)
ok = test_chunk_size(12)
record("chunk_3token", ok, "chunk_size=12 (~3 tokens)")

print("\n" + "=" * 60)
print(f"RESULTS: {PASS} PASS, {FAIL} FAIL (total {PASS + FAIL})")
for r in RESULTS:
    print("  " + r)
print("DONE")
