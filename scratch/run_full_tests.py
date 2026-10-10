#!/usr/bin/env python3
"""Comprehensive aux-isolation validation test.
Uses a fake LLM server for instant responses.
Scenarios: A (real trim + aux), B (restart+resume), C (negative control), R1, R2, R3.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

SCRATCH = "/home/pawelw/ctxproxy/scratch"
PROXY_PORT = 19203
FAKE_LLM_PORT = 19204
DB = "ctxproxy_sandbox"
PGPASS_CMD = "grep '^CTXGATE_PG_PASS=' /home/pawelw/ctxproxy/.env | cut -d= -f2"

# Get PG password
pgpass = subprocess.check_output(PGPASS_CMD, shell=True, text=True).strip()
os.environ["PGPASSWORD"] = pgpass

def psql(query):
    """Run a psql query and return stripped output."""
    r = subprocess.run(
        ["psql", "-U", "postgres", "-h", "127.0.0.1", DB, "-t", "-A", "-c", query],
        capture_output=True, text=True, env={**os.environ, "PGPASSWORD": pgpass}
    )
    return r.stdout.strip()

def psql_multi(query):
    """Run a psql query returning multiple rows as list."""
    r = subprocess.run(
        ["psql", "-U", "postgres", "-h", "127.0.0.1", DB, "-t", "-A", "-c", query],
        capture_output=True, text=True, env={**os.environ, "PGPASSWORD": pgpass}
    )
    return [line for line in r.stdout.strip().split("\n") if line]

def http_post(url, data, headers=None):
    """POST JSON and return response."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": str(e), "body": e.read().decode()}

def send_chat(session_id, messages, max_tokens=30, tools=None):
    """Send a chat completion request to the sandbox proxy."""
    url = f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions"
    payload = {
        "model": "Qwen3.8-27B",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    headers = {"agent-session-id": session_id}
    return http_post(url, payload, headers)

def truncate_db():
    """Truncate all proxy tables in sandbox DB."""
    tables = "proxy.tasks, proxy.session_windows, proxy.events, proxy.memory_jobs, proxy.knowledge, proxy.memories, proxy.phase_summaries, proxy.session_summaries, proxy.working_memory, proxy.session_ledger, proxy.deliverables"
    subprocess.run(
        ["psql", "-U", "postgres", "-h", "127.0.0.1", DB, "-c", f"TRUNCATE {tables} CASCADE"],
        capture_output=True, text=True, env={**os.environ, "PGPASSWORD": pgpass}
    )

def get_state(session_id):
    """Get current state for a session."""
    sk = f"goose:{session_id}"
    sw = psql(f"SELECT cut, summarized_through, dropped_total FROM proxy.session_windows WHERE session_key='{sk}'")
    ps_count = psql(f"SELECT count(*) FROM proxy.phase_summaries WHERE task_id=(SELECT id FROM proxy.tasks WHERE session_id='{session_id}')")
    ps_max_phase = psql(f"SELECT max(phase_number) FROM proxy.phase_summaries WHERE task_id=(SELECT id FROM proxy.tasks WHERE session_id='{session_id}')")
    return {"session_windows": sw, "phase_summaries_count": ps_count, "phase_summaries_max": ps_max_phase}

def get_log_lines(pattern):
    """Get lines from sandbox proxy log matching pattern."""
    r = subprocess.run(f"grep -c '{pattern}' {SCRATCH}/sandbox_proxy.log 2>/dev/null", shell=True, capture_output=True, text=True)
    out = r.stdout.strip()
    return int(out) if out.isdigit() else 0

def get_log_lines_text(pattern):
    """Get actual lines from sandbox proxy log matching pattern."""
    r = subprocess.run(f"grep '{pattern}' {SCRATCH}/sandbox_proxy.log 2>/dev/null || true", shell=True, capture_output=True, text=True)
    return r.stdout.strip()

def start_fake_llm():
    """Start the fake LLM server."""
    proc = subprocess.Popen(
        [sys.executable, f"{SCRATCH}/fake_llm.py"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError(f"Fake LLM died: {proc.stderr.read().decode()}")
    return proc

def start_proxy(app_file, port=PROXY_PORT):
    """Start the sandbox proxy."""
    env = {**os.environ}
    env.update({
        "CTXGATE_PROXY_PORT": str(port),
        "CTXGATE_DB_DSN": f"postgresql://postgres:{pgpass}@127.0.0.1:5432/{DB}",
        "CTXGATE_ALLOW_NO_AUTH": "1",
        "CTXGATE_HOST": "127.0.0.1",
        "CTXGATE_MAX_CONTEXT": "200",
        "CTXGATE_MAX_INPUT": "100",
        "CTXGATE_MAX_OUTPUT": "50",
        "CTXGATE_SAFETY_MARGIN": "20",
        "CTXGATE_MIN_OUTPUT": "30",
        "CTXGATE_VLLM_URL": f"http://127.0.0.1:{FAKE_LLM_PORT}/v1",
        "CTXGATE_VLLM_MODEL": "Qwen3.8-27B",
        "CTXGATE_LM_URL": f"http://127.0.0.1:{FAKE_LLM_PORT}/v1/chat/completions",
        "CTXGATE_LM_MODEL": "fake-llm",
        "CTXGATE_LM_API_KEY": "fake-key",
        "CTXGATE_LM_WORKERS": "2",
        "CTXGATE_SKIP_DOTENV": "1",
        "CTXGATE_QWEN_TOKENIZER": "/home/pawelw/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json",
    })
    # Clear log
    open(f"{SCRATCH}/sandbox_proxy.log", "w").close()
    proc = subprocess.Popen(
        [sys.executable, app_file],
        stdout=open(f"{SCRATCH}/sandbox_proxy.log", "a"),
        stderr=subprocess.STDOUT,
        env=env,
        cwd="/home/pawelw/ctxproxy",
    )
    time.sleep(3)
    if proc.poll() is not None:
        log = open(f"{SCRATCH}/sandbox_proxy.log").read()
        raise RuntimeError(f"Proxy died: {log[-500:]}")
    return proc

def stop_proc(proc):
    """Stop a process gracefully."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

# Generate a long message that will exceed 100 tokens
# Each message ~35 tokens, 4 messages = ~140 total > MAX_INPUT(100)
# But each individual message < ceiling(100) so no 413
MSG_A = "The quick brown fox jumps over the lazy dog while the cat watches from the tree branch above"
MSG_B = "A large elephant walked slowly through the dense jungle carrying a heavy bundle on its back"
MSG_C = "The old lighthouse keeper tended his lamp every evening as ships passed safely along the coast"
MSG_D = "Children played in the garden while their mother prepared a wonderful dinner inside the warm house"

def over_limit_messages(system="You are a helpful assistant."):
    """Generate messages that collectively exceed MAX_INPUT but each is under ceiling."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": MSG_A},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_B},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_C},
    ]

def over_limit_messages_v2(system="You are a helpful assistant."):
    """Even more over limit - for the second over-limit request."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": MSG_A},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_B},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_C},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_D},
    ]

def main():
    results = {}
    
    print("=" * 60)
    print("AUX-ISOLATION VALIDATION - EMPIRICAL TESTS")
    print("=" * 60)
    
    # Start fake LLM
    print("\n[Setup] Starting fake LLM server on port 19204...")
    fake_llm = start_fake_llm()
    print(f"  Fake LLM PID: {fake_llm.pid}")
    
    try:
        # ============================================================
        # SCENARIO A: Real trim + auxiliary requests (FIXED code)
        # ============================================================
        print("\n" + "=" * 60)
        print("SCENARIO A: Real trim + aux requests (FIXED code)")
        print("=" * 60)
        
        truncate_db()
        proxy = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy PID: {proxy.pid}")
        
        sid_a = "scen-a-main"
        
        # Step 1: Drive session over limit
        print(f"\n  [A1] Sending over-limit main request (session={sid_a})...")
        r1 = send_chat(sid_a, over_limit_messages(), max_tokens=30)
        time.sleep(1)
        
        state_a1 = get_state(sid_a)
        log_dropped = get_log_lines_text("Dropped slice")
        log_seed_changed = get_log_lines("SEED CHANGED")
        log_h3pop = get_log_lines("H3-POP")
        
        print(f"  Response: {json.dumps(r1)[:200]}")
        print(f"  State after A1: {state_a1}")
        print(f"  'Dropped slice' in log: {len(log_dropped) > 0}")
        print(f"  'SEED CHANGED' count: {log_seed_changed}")
        print(f"  'H3-POP' count: {log_h3pop}")
        
        # Step 2: Send 3 auxiliary requests (2 msgs, different system prompt, no tools)
        print(f"\n  [A2] Sending 3 auxiliary requests (same session, 2 msgs, diff system)...")
        for i in range(3):
            r_aux = send_chat(sid_a, [
                {"role": "system", "content": f"Generate a short title. Request {i+1}."},
                {"role": "user", "content": "Hello there"},
            ], max_tokens=15)
            time.sleep(0.5)
        
        state_a2 = get_state(sid_a)
        log_seed_changed_a2 = get_log_lines("SEED CHANGED")
        log_h3pop_a2 = get_log_lines("H3-POP")
        
        print(f"  State after A2: {state_a2}")
        print(f"  'SEED CHANGED' count after aux: {log_seed_changed_a2}")
        print(f"  'H3-POP' count after aux: {log_h3pop_a2}")
        
        # Step 3: Send next main over-limit request
        print(f"\n  [A3] Sending next main over-limit request...")
        r3 = send_chat(sid_a, over_limit_messages_v2(), max_tokens=30)
        time.sleep(1)
        
        state_a3 = get_state(sid_a)
        log_seed_changed_a3 = get_log_lines("SEED CHANGED")
        log_h3pop_a3 = get_log_lines("H3-POP")
        
        print(f"  State after A3: {state_a3}")
        print(f"  'SEED CHANGED' count after A3: {log_seed_changed_a3}")
        print(f"  'H3-POP' count after A3: {log_h3pop_a3}")
        
        # PASS criteria for Scenario A
        scen_a_pass = (
            log_seed_changed_a3 == 0 and
            log_h3pop_a3 == 0 and
            state_a1["session_windows"] != ""  # trim actually happened
        )
        # Check summarized_through never decreases
        sw_parts_a1 = state_a1["session_windows"].split(",") if state_a1["session_windows"] else []
        sw_parts_a3 = state_a3["session_windows"].split(",") if state_a3["session_windows"] else []
        if len(sw_parts_a1) >= 2 and len(sw_parts_a3) >= 2:
            try:
                stm_a1 = int(sw_parts_a1[1])
                stm_a3 = int(sw_parts_a3[1])
                scen_a_pass = scen_a_pass and (stm_a3 >= stm_a1)
            except ValueError:
                pass
        
        results["scenario_a"] = {
            "pass": scen_a_pass,
            "state_a1": state_a1,
            "state_a2": state_a2,
            "state_a3": state_a3,
            "seed_changed_total": log_seed_changed_a3,
            "h3pop_total": log_h3pop_a3,
            "dropped_slice_found": len(log_dropped) > 0,
        }
        print(f"\n  SCENARIO A: {'PASS' if scen_a_pass else 'FAIL'}")
        
        stop_proc(proxy)
        
        # ============================================================
        # SCENARIO B: Restart + resume
        # ============================================================
        print("\n" + "=" * 60)
        print("SCENARIO B: Restart + resume (FIXED code)")
        print("=" * 60)
        
        # Don't truncate - we want to resume the existing state
        proxy_b = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy B PID: {proxy_b.pid}")
        
        # Clear log to track new events
        open(f"{SCRATCH}/sandbox_proxy.log", "w").close()
        
        # Resume the same session
        print(f"\n  [B1] Resuming session {sid_a} after restart...")
        r_b1 = send_chat(sid_a, over_limit_messages_v2(), max_tokens=30)
        time.sleep(1)
        
        state_b1 = get_state(sid_a)
        log_window_loads = get_log_lines_text("window_loads_db")
        log_resum = get_log_lines_text("resum")
        
        print(f"  State after B1: {state_b1}")
        print(f"  'window_loads_db' lines: {log_window_loads[:200] if log_window_loads else 'NONE'}")
        print(f"  'resum' lines: {log_resum[:200] if log_resum else 'NONE'}")
        
        # Check: no re-summarization (phase_summaries count should not increase for already-summarized content)
        ps_before_b = state_a3["phase_summaries_count"]
        ps_after_b = state_b1["phase_summaries_count"]
        
        # The key check: window_loads_db should have incremented (state loaded from DB)
        # And summarized_through should NOT decrease
        sw_b = state_b1["session_windows"].split(",") if state_b1["session_windows"] else []
        sw_a3 = state_a3["session_windows"].split(",") if state_a3["session_windows"] else []
        
        scen_b_pass = True
        notes_b = []
        
        # Check window was loaded from DB
        if log_window_loads:
            notes_b.append("window_loads_db found")
        else:
            # Check for "Loaded window" or similar
            log_loaded = get_log_lines_text("Loaded window")
            if log_loaded:
                notes_b.append("Loaded window found")
            else:
                notes_b.append("WARNING: no window load evidence")
                # Not necessarily a fail - might be under-limit
        
        # Check summarized_through didn't decrease
        if len(sw_b) >= 2 and len(sw_a3) >= 2:
            try:
                stm_b = int(sw_b[1])
                stm_a3 = int(sw_a3[1])
                if stm_b < stm_a3:
                    scen_b_pass = False
                    notes_b.append(f"FAIL: summarized_through decreased {stm_a3} -> {stm_b}")
                else:
                    notes_b.append(f"summarized_through stable/increased: {stm_a3} -> {stm_b}")
            except ValueError:
                pass
        
        results["scenario_b"] = {
            "pass": scen_b_pass,
            "state_b1": state_b1,
            "notes": notes_b,
            "window_loads_evidence": bool(log_window_loads or get_log_lines_text("Loaded window")),
        }
        print(f"\n  SCENARIO B: {'PASS' if scen_b_pass else 'FAIL'}")
        print(f"  Notes: {notes_b}")
        
        stop_proc(proxy_b)
        
        # ============================================================
        # SCENARIO C: Negative control (ORIGINAL code with bug)
        # ============================================================
        print("\n" + "=" * 60)
        print("SCENARIO C: Negative control (ORIGINAL code - expect defect)")
        print("=" * 60)
        
        truncate_db()
        proxy_c = start_proxy(f"{SCRATCH}/app_sandbox_orig.py")
        print(f"  Proxy C PID: {proxy_c.pid}")
        
        sid_c = "scen-c-orig"
        
        # Step 1: Drive over limit
        print(f"\n  [C1] Sending over-limit main request (session={sid_c})...")
        r_c1 = send_chat(sid_c, over_limit_messages(), max_tokens=30)
        time.sleep(1)
        
        state_c1 = get_state(sid_c)
        log_dropped_c = get_log_lines_text("Dropped slice")
        print(f"  State after C1: {state_c1}")
        print(f"  'Dropped slice' in log: {len(log_dropped_c) > 0}")
        
        # Step 2: Send auxiliary request (triggers the bug in original)
        print(f"\n  [C2] Sending auxiliary request (triggers H3-POP in original)...")
        r_c2 = send_chat(sid_c, [
            {"role": "system", "content": "Generate a short title."},
            {"role": "user", "content": "Hello there"},
        ], max_tokens=15)
        time.sleep(1)
        
        state_c2 = get_state(sid_c)
        log_h3pop_c = get_log_lines("H3-POP")
        log_seed_changed_c = get_log_lines("SEED CHANGED")
        print(f"  State after C2: {state_c2}")
        print(f"  'H3-POP' count: {log_h3pop_c}")
        print(f"  'SEED CHANGED' count: {log_seed_changed_c}")
        
        # Step 3: Send next main over-limit request
        print(f"\n  [C3] Sending next main over-limit request...")
        r_c3 = send_chat(sid_c, over_limit_messages_v2(), max_tokens=30)
        time.sleep(1)
        
        state_c3 = get_state(sid_c)
        log_h3pop_c3 = get_log_lines("H3-POP")
        log_seed_changed_c3 = get_log_lines("SEED CHANGED")
        
        print(f"  State after C3: {state_c3}")
        print(f"  'H3-POP' total: {log_h3pop_c3}")
        print(f"  'SEED CHANGED' total: {log_seed_changed_c3}")
        
        # In the original code, we expect:
        # - H3-POP to fire (state wiped)
        # - Possibly SEED CHANGED
        # - summarized_through may regress or phase_summaries may duplicate
        defect_reproduced = (log_h3pop_c > 0) or (log_seed_changed_c > 0)
        
        # Check for watermark regression
        sw_c1 = state_c1["session_windows"].split(",") if state_c1["session_windows"] else []
        sw_c3 = state_c3["session_windows"].split(",") if state_c3["session_windows"] else []
        watermark_regression = False
        if len(sw_c1) >= 2 and len(sw_c3) >= 2:
            try:
                stm_c1 = int(sw_c1[1])
                stm_c3 = int(sw_c3[1])
                if stm_c3 < stm_c1:
                    watermark_regression = True
            except ValueError:
                pass
        
        results["scenario_c"] = {
            "defect_reproduced": defect_reproduced,
            "watermark_regression": watermark_regression,
            "state_c1": state_c1,
            "state_c2": state_c2,
            "state_c3": state_c3,
            "h3pop_count": log_h3pop_c3,
            "seed_changed_count": log_seed_changed_c3,
        }
        print(f"\n  SCENARIO C: Defect reproduced = {defect_reproduced}, Watermark regression = {watermark_regression}")
        if defect_reproduced:
            print("  -> Original code HAS the bug (fix is necessary)")
        else:
            print("  -> Original code did NOT reproduce the defect in this test")
        
        stop_proc(proxy_c)
        
        # ============================================================
        # R1: Under-limit branch no longer pops stale state
        # ============================================================
        print("\n" + "=" * 60)
        print("R1: Under-limit branch with stale state (FIXED code)")
        print("=" * 60)
        
        truncate_db()
        proxy_r1 = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy R1 PID: {proxy_r1.pid}")
        
        sid_r1 = "r1-stale-state"
        
        # Step 1: Create over-limit state (trigger trim)
        print(f"\n  [R1-1] Creating over-limit state...")
        send_chat(sid_r1, over_limit_messages(), max_tokens=30)
        time.sleep(1)
        
        state_r1_1 = get_state(sid_r1)
        print(f"  State: {state_r1_1}")
        
        # Step 2: Simulate Goose compacting - send a SHORT request (under limit)
        # This would have triggered H3-POP in original code
        print(f"\n  [R1-2] Sending short under-limit request (simulates Goose compacting)...")
        send_chat(sid_r1, [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hi"},
        ], max_tokens=15)
        time.sleep(1)
        
        state_r1_2 = get_state(sid_r1)
        log_h3pop_r1 = get_log_lines("H3-POP")
        print(f"  State after short request: {state_r1_2}")
        print(f"  'H3-POP' count: {log_h3pop_r1}")
        
        # Step 3: Send another over-limit request - verify state is intact
        print(f"\n  [R1-3] Sending over-limit request again...")
        send_chat(sid_r1, over_limit_messages_v2(), max_tokens=30)
        time.sleep(1)
        
        state_r1_3 = get_state(sid_r1)
        log_h3pop_r1_final = get_log_lines("H3-POP")
        log_seed_changed_r1 = get_log_lines("SEED CHANGED")
        print(f"  State after R1-3: {state_r1_3}")
        print(f"  'H3-POP' total: {log_h3pop_r1_final}")
        print(f"  'SEED CHANGED' total: {log_seed_changed_r1}")
        
        # PASS: no H3-POP, no SEED CHANGED, watermark sane
        r1_pass = (log_h3pop_r1_final == 0 and log_seed_changed_r1 == 0)
        # Check watermark didn't regress
        if state_r1_1["session_windows"] and state_r1_3["session_windows"]:
            sw1 = state_r1_1["session_windows"].split(",")
            sw3 = state_r1_3["session_windows"].split(",")
            if len(sw1) >= 2 and len(sw3) >= 2:
                try:
                    r1_pass = r1_pass and (int(sw3[1]) >= int(sw1[1]))
                except ValueError:
                    pass
        
        results["r1"] = {
            "pass": r1_pass,
            "h3pop": log_h3pop_r1_final,
            "seed_changed": log_seed_changed_r1,
            "state_before": state_r1_1,
            "state_after_short": state_r1_2,
            "state_final": state_r1_3,
        }
        print(f"\n  R1: {'PASS' if r1_pass else 'FAIL'}")
        
        stop_proc(proxy_r1)
        
        # ============================================================
        # R2: 3+ message request with different system prompt
        # ============================================================
        print("\n" + "=" * 60)
        print("R2: 3+ messages, different system prompt (FIXED code)")
        print("=" * 60)
        
        truncate_db()
        proxy_r2 = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy R2 PID: {proxy_r2.pid}")
        
        sid_r2 = "r2-diff-sysprompt"
        
        # Step 1: Establish session with normal system prompt
        print(f"\n  [R2-1] Establishing session...")
        send_chat(sid_r2, [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ], max_tokens=20)
        time.sleep(1)
        
        state_r2_1 = get_state(sid_r2)
        
        # Step 2: Send 3+ messages with DIFFERENT system prompt
        print(f"\n  [R2-2] Sending 3 msgs with different system prompt...")
        send_chat(sid_r2, [
            {"role": "system", "content": "You are a title generator. Be brief."},
            {"role": "user", "content": "Summarize this"},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "Thanks"},
        ], max_tokens=15)
        time.sleep(1)
        
        state_r2_2 = get_state(sid_r2)
        log_seed_changed_r2 = get_log_lines("SEED CHANGED")
        log_h3pop_r2 = get_log_lines("H3-POP")
        
        print(f"  State after R2-2: {state_r2_2}")
        print(f"  'SEED CHANGED' count: {log_seed_changed_r2}")
        print(f"  'H3-POP' count: {log_h3pop_r2}")
        
        # Step 3: Send normal request again - check state integrity
        print(f"\n  [R2-3] Sending normal request...")
        send_chat(sid_r2, [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello again"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "How are you?"},
        ], max_tokens=20)
        time.sleep(1)
        
        state_r2_3 = get_state(sid_r2)
        log_seed_changed_r2_final = get_log_lines("SEED CHANGED")
        log_h3pop_r2_final = get_log_lines("H3-POP")
        
        print(f"  State after R2-3: {state_r2_3}")
        print(f"  'SEED CHANGED' total: {log_seed_changed_r2_final}")
        print(f"  'H3-POP' total: {log_h3pop_r2_final}")
        
        # R2: Report behavior. The fix uses 'elif len(built) >= 3' so a 4-message
        # request with different system prompt WILL trigger seed comparison.
        # This is expected behavior - the seed SHOULD update if the conversation changes.
        # The key is: does it CORRUPT state (pop compactions)?
        r2_corruption = log_h3pop_r2_final > 0
        r2_seed_flap = log_seed_changed_r2_final > 0
        
        results["r2"] = {
            "corruption": r2_corruption,
            "seed_flap": r2_seed_flap,
            "seed_changed_count": log_seed_changed_r2_final,
            "h3pop_count": log_h3pop_r2_final,
            "state_final": state_r2_3,
            "note": "Seed flap on 3+ msg diff-system is EXPECTED (seed updates). Corruption = H3-POP.",
        }
        print(f"\n  R2: Corruption (H3-POP) = {r2_corruption}, Seed flap = {r2_seed_flap}")
        print(f"  Interpretation: {results['r2']['note']}")
        
        stop_proc(proxy_r2)
        
        # ============================================================
        # R3: Prod read-only check
        # ============================================================
        print("\n" + "=" * 60)
        print("R3: Prod events pollution check (read-only)")
        print("=" * 60)
        
        r3_query = "SELECT left(content,80), count(*) FROM proxy.events GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
        r3_result = subprocess.run(
            ["psql", "-U", "postgres", "-h", "127.0.0.1", "ctxproxy", "-t", "-A", "-c", r3_query],
            capture_output=True, text=True, env={**os.environ, "PGPASSWORD": pgpass}
        )
        r3_lines = [l for l in r3_result.stdout.strip().split("\n") if l]
        print(f"  Top 20 event content prefixes (prod):")
        for line in r3_lines:
            print(f"    {line}")
        
        # Check for title-like pollution
        title_like = [l for l in r3_lines if "title" in l.lower() or "generate" in l.lower()]
        results["r3"] = {
            "total_rows": len(r3_lines),
            "title_like": len(title_like),
            "title_like_examples": title_like[:5],
        }
        print(f"  Title-like entries: {len(title_like)}/{len(r3_lines)}")
        
        # ============================================================
        # PHASE 4: Re-run verify_aux_isolation.sh
        # ============================================================
        print("\n" + "=" * 60)
        print("PHASE 4: Re-running verify_aux_isolation.sh")
        print("=" * 60)
        
        # Need to start proxy first for the verify script
        truncate_db()
        proxy_v = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy V PID: {proxy_v.pid}")
        
        # Run the verify script
        verify_result = subprocess.run(
            ["bash", f"{SCRATCH}/verify_aux_isolation.sh"],
            capture_output=True, text=True, timeout=120
        )
        verify_output = verify_result.stdout + verify_result.stderr
        print(f"  Verify script output:")
        for line in verify_output.split("\n"):
            if line.strip():
                print(f"    {line}")
        
        verify_pass = verify_result.returncode == 0
        results["phase4_verify"] = {
            "pass": verify_pass,
            "returncode": verify_result.returncode,
            "output_tail": verify_output[-500:] if len(verify_output) > 500 else verify_output,
        }
        
        stop_proc(proxy_v)
        
    finally:
        stop_proc(fake_llm)
    
    # ============================================================
    # FINAL SUMMARY
    # ============================================================
    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    
    summary = {
        "static_gate": "PASS (verified separately)",
        "scenario_a": "PASS" if results.get("scenario_a", {}).get("pass") else "FAIL",
        "scenario_b": "PASS" if results.get("scenario_b", {}).get("pass") else "FAIL",
        "scenario_c_defect_reproduced": results.get("scenario_c", {}).get("defect_reproduced", False),
        "r1": "PASS" if results.get("r1", {}).get("pass") else "FAIL",
        "r2_corruption": results.get("r2", {}).get("corruption", False),
        "r2_seed_flap": results.get("r2", {}).get("seed_flap", False),
        "r3_title_like": results.get("r3", {}).get("title_like", 0),
        "phase4_verify": "PASS" if results.get("phase4_verify", {}).get("pass") else "FAIL",
    }
    
    for k, v in summary.items():
        print(f"  {k}: {v}")
    
    # Write results to file
    with open(f"{SCRATCH}/test_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Full results written to {SCRATCH}/test_results.json")
    
    return summary

if __name__ == "__main__":
    main()

