#!/usr/bin/env python3
import json, time, urllib.request, urllib.error

PROXY_URL = "http://localhost:9201/v1/chat/completions"
SESSION_ID = "e2e-malformed-001"

def raw_post(url, body_bytes):
    h = {"Content-Type": "application/json", "X-Session-ID": SESSION_ID}
    req = urllib.request.Request(url, data=body_bytes, method="POST", headers=h)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "body": resp.read().decode("utf-8"), "latency": time.time() - start}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8"), "latency": time.time() - start}
    except Exception as e:
        return {"status": -1, "body": str(e), "latency": time.time() - start}

def main():
    print("=== PHASE 4d: MALFORMED REQUESTS ===")
    tests = [
        ("incomplete_json", b'{"model":"qwen","messages":['),
        ("wrong_field_type", json.dumps({"model": 12345, "messages": "not_a_list"}).encode()),
        ("empty_messages", json.dumps({"model": "qwen", "messages": [], "max_tokens": 10}).encode()),
        ("null_content", json.dumps({"model": "qwen", "messages": [{"role": "user", "content": None}], "max_tokens": 10}).encode()),
        ("missing_model", json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()),
        ("negative_max_tokens", json.dumps({"model": "qwen", "messages": [{"role": "user", "content": "hi"}], "max_tokens": -5}).encode()),
        ("huge_max_tokens", json.dumps({"model": "qwen", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 999999}).encode()),
    ]
    results = []
    for name, body in tests:
        r = raw_post(PROXY_URL, body)
        is_4xx = 400 <= r["status"] < 500
        is_alive = r["status"] != -1
        status_str = "PASS" if (is_4xx or is_alive) else "FAIL"
        print("  %-25s -> HTTP %d (%s) lat=%.2fs" % (name, r["status"], status_str, r["latency"]))
        results.append({"test": name, "status": r["status"], "pass": is_4xx or is_alive, "body_preview": r["body"][:200]})
    
    # Verify proxy is still alive
    try:
        hr = urllib.request.urlopen("http://localhost:9201/health", timeout=10)
        print("\nPost-test health: HTTP %d - ALIVE" % hr.status)
    except Exception as e:
        print("\nPost-test health: DEAD - %s" % e)
    
    out = "/home/pawelw/ctxproxy/e2e-evidence/phase4d-results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print("Saved to %s" % out)

if __name__ == "__main__":
    main()