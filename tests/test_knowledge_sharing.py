"""Suite 18: test_knowledge_sharing.py - Cross-session knowledge sharing tests"""
import os
import subprocess
import sys
import time

import httpx

PROXY = "http://127.0.0.1:9201"
PGENV = "PGPASSWORD=" + os.environ.get("CTXGATE_PG_PASS", "postgres") + " psql -h 127.0.0.1 -U postgres -d local-llm-ctxgate-proxy -t"
PASS = 0
FAIL = 0
FAILURES = []

def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        msg = f"  FAIL: {name} {detail}"
        FAILURES.append(msg)
        print(msg)

def pg_query(sql: str) -> str:
    cmd = f"{PGENV} -c \"{sql}\""
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    return r.stdout.strip()

def api_post(path, body, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return httpx.post(PROXY + path, json=body, headers=h, timeout=30)

def api_get(path, params=None, headers=None):
    return httpx.get(PROXY + path, params=params, headers=headers or {}, timeout=30)

def chat(messages, x_sid):
    body = {"model": "Qwen3.8-27B", "messages": messages, "max_tokens": 50}
    headers = {"X-Session-ID": x_sid, "Content-Type": "application/json"}
    return httpx.post(PROXY + "/v1/chat/completions", json=body, headers=headers, timeout=60)

print("=" * 60)
print("SUITE 18: CROSS-SESSION KNOWLEDGE SHARING")
print("=" * 60)

# --- K1: Create knowledge item via API ---
print("\nK1: POST /knowledge creates a global knowledge item")
r = api_post("/knowledge", {"domain": "config", "key": "test_port", "value": "9201", "importance": 8})
check("K1a: 200 response", r.status_code == 200)
check("K1b: status ok", r.json().get("status") == "ok")

# --- K2: Search finds the item ---
print("\nK2: GET /knowledge/search finds by keyword")
r = api_get("/knowledge/search", {"q": "test_port", "limit": 10})
check("K2a: 200 response", r.status_code == 200)
data = r.json()
check("K2b: found at least 1", data.get("count", 0) >= 1)
found = [i for i in data.get("items", []) if i["key"] == "test_port"]
check("K2c: correct item found", len(found) >= 1)

# --- K3: Search by domain ---
print("\nK3: GET /knowledge/search?domain=config")
r = api_get("/knowledge/search", {"domain": "config", "limit": 10})
check("K3a: 200 response", r.status_code == 200)
data = r.json()
check("K3b: has items", data.get("count", 0) >= 1)
all_config = all(i["domain"] == "config" for i in data.get("items", []))
check("K3c: all items are config domain", all_config)

# --- K4: Stats endpoint ---
print("\nK4: GET /knowledge/stats")
r = api_get("/knowledge/stats")
check("K4a: 200 response", r.status_code == 200)
data = r.json()
check("K4b: total >= 1", data.get("total", 0) >= 1)
check("K4c: by_domain is list", isinstance(data.get("by_domain"), list))

# --- K5: Upsert ---
print("\nK5: POST same domain+key upserts (no duplicate)")
r1 = api_post("/knowledge", {"domain": "config", "key": "upsert_test", "value": "original", "importance": 5})
r2 = api_post("/knowledge", {"domain": "config", "key": "upsert_test", "value": "updated", "importance": 7})
check("K5a: both 200", r1.status_code == 200 and r2.status_code == 200)
r = api_get("/knowledge/search", {"q": "upsert_test", "limit": 10})
items = [i for i in r.json().get("items", []) if i["key"] == "upsert_test"]
check("K5b: exactly 1 item (upsert)", len(items) == 1)
check("K5c: value updated", len(items) > 0 and items[0]["value"] == "updated")
check("K5d: importance is max(5,7)=7", len(items) > 0 and items[0]["importance"] == 7)

# --- K6: Cross-session chat extracts knowledge ---
print("\nK6: Chat in session A extracts + stores knowledge")
msgs_a = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "The proxy port is 9201 and the vLLM url is http://127.0.0.1:29000/v1. We decided to use Qwen3.8-27B model."}
]
r = chat(msgs_a, "kn-test-A")
check("K6a: 200 response", r.status_code == 200)
time.sleep(3)
r = api_get("/knowledge/search", {"q": "9201", "limit": 10})
items_9201 = [i for i in r.json().get("items", []) if "9201" in i["value"] or "9201" in i["key"]]
check("K6b: port 9201 knowledge stored", len(items_9201) >= 1)

r = api_get("/knowledge/search", {"q": "29000", "limit": 10})
items_29000 = [i for i in r.json().get("items", []) if "29000" in i["value"] or "29000" in i["key"]]
check("K6c: vLLM port knowledge stored", len(items_29000) >= 1)

# --- K7: Cross-session knowledge injection ---
print("\nK7: Session B request succeeds with knowledge injection")
msgs_b = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What port is the proxy running on?"}
]
r = chat(msgs_b, "kn-test-B")
check("K7a: 200 response (knowledge injected into system prompt)", r.status_code == 200)

# --- K8: Knowledge is global (no task_id FK) ---
print("\nK8: Knowledge is global (no task_id column)")
cnt = pg_query("SELECT COUNT(*) FROM proxy.knowledge WHERE active = true")
cnt_val = int(cnt) if cnt.isdigit() else 0
check("K8a: global table has items", cnt_val >= 3, f"(count={cnt_val})")

has_task_id = pg_query("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='proxy' AND table_name='knowledge' AND column_name='task_id'")
no_task_id = has_task_id == "0"
check("K8b: no task_id column (global, not per-session)", no_task_id)

# --- K9: Source tracking ---
print("\nK9: Knowledge items track source session")
r = api_get("/knowledge/search", {"q": "model", "limit": 10})
items = r.json().get("items", [])
has_source = any(i.get("source_session") for i in items)
check("K9a: source_session recorded on extracted items", has_source)

# --- K10: Empty search ---
print("\nK10: Empty knowledge search returns valid JSON")
r = api_get("/knowledge/search", {"q": "zzzznonexistentzzzz", "limit": 5})
check("K10a: 200 response", r.status_code == 200)
data = r.json()
check("K10b: count is 0", data.get("count", -1) == 0)
check("K10c: items is empty list", data.get("items") == [])

# --- K11: Validation ---
print("\nK11: POST /knowledge requires key and value")
r = api_post("/knowledge", {"domain": "test"})
check("K11a: 400 without key/value", r.status_code == 400)

print("\n" + "=" * 60)
print(f"RESULTS: {PASS} PASS, {FAIL} FAIL")
if FAILURES:
    print("\nFailures:")
    for f in FAILURES:
        print(f"  {f}")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
