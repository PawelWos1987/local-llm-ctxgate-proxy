#!/usr/bin/env python3
"""Phase 1b golden test harness - sends scenarios to already-running proxy."""
import json
import os
import time
import sys
import urllib.request

PROXY_PORT = 9301
LOG_FILE = "/tmp/ctxgate_p1b_proxy2.log"
RESULTS_FILE = "/home/pawelw/ctxproxy-dev/tests/golden_results.json"

SCENARIOS = [
    ("a_normal_tool_call", "normal_tool_call"),
    ("b_text_length_continuation", "text_length_continuation"),
    ("c_reasoning_backstop", "reasoning_backstop"),
    ("d_reasoning_loop", "reasoning_loop"),
    ("e_content_loop_first", "content_loop_first"),
    ("e2_content_loop_after_tool", "content_loop_after_tool"),
    ("f_tool_call_truncated", "tool_call_truncated"),
    ("g_stream_interrupted", "stream_interrupted"),
    ("h_retry_length", "retry_length"),
]

def make_messages(scenario):
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Please do the task for scenario: " + scenario},
    ]

def send_scenario(name, scenario):
    url = "http://127.0.0.1:%d/v1/chat/completions" % PROXY_PORT
    body = {
        "model": "Qwen3.8-27B",
        "messages": make_messages(scenario),
        "scenario": scenario,
        "stream": True,
        "max_tokens": 200,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Session-ID": "p1b-" + name},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        raw = resp.read().decode("utf-8", errors="replace")
        status = resp.status
    except Exception as e:
        raw = "EXCEPTION: " + str(e)
        status = 0
    return {"name": name, "scenario": scenario, "status": status, "sse": raw}

def parse_nsdiag(log_text, session_prefix):
    lines = []
    for line in log_text.split("\n"):
        if "NS-DIAG" in line and session_prefix in line:
            lines.append(line.strip())
    return lines

def main():
    results = []
    for name, scenario in SCENARIOS:
        r = send_scenario(name, scenario)
        results.append(r)
        print("Scenario %s: status=%d, sse_len=%d" % (name, r["status"], len(r["sse"])))
        time.sleep(1)
    
    time.sleep(2)
    
    with open(LOG_FILE, "r") as f:
        log_text = f.read()
    
    for r in results:
        r["nsdiag"] = parse_nsdiag(log_text, "p1b-" + r["name"])
    
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)
    print("Results saved to %s" % RESULTS_FILE)
    
    print("\n=== NS-DIAG SUMMARY ===")
    for r in results:
        for line in r["nsdiag"]:
            print("[%s] %s" % (r["name"], line[:200]))
        if not r["nsdiag"]:
            print("[%s] (no NS-DIAG line found)" % r["name"])

if __name__ == "__main__":
    main()
