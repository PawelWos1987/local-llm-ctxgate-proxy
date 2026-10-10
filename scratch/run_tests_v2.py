#!/usr/bin/env python3
"""AUX-ISOLATION VALIDATION - EMPIRICAL TESTS (v2)
Uses minimal env (no os.environ inheritance) to ensure clean config.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

SCRATCH = "/home/pawelw/ctxproxy/scratch"
PROXY_PORT = 19203
FAKE_LLM_PORT = 19204
DB = "ctxproxy_sandbox"

def get_pgpass():
    return subprocess.check_output(
        "grep '^CTXGATE_PG_PASS=' /home/pawelw/ctxproxy/.env | cut -d= -f2",
        shell=True, text=True
    ).strip()

PGPASS = get_pgpass()

def psql(query, db=DB):
    r = subprocess.run(
        ["psql", "-U", "postgres", "-h", "127.0.0.1", db, "-t", "-A", "-c", query],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PGPASSWORD": PGPASS}
    )
    return r.stdout.strip()

def http_post(url, data, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": str(e), "body": e.read().decode()}

def send_chat(session_id, messages, max_tokens=30):
    url = f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions"
    payload = {
        "model": "Qwen3.8-27B",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    return http_post(url, payload, {"agent-session-id": session_id})

def truncate_db():
    tables = "proxy.tasks, proxy.session_windows, proxy.events, proxy.memory_jobs, proxy.knowledge, proxy.memories, proxy.phase_summaries, proxy.session_summaries, proxy.working_memory, proxy.session_ledger, proxy.deliverables"
    subprocess.run(
        ["psql", "-U", "postgres", "-h", "127.0.0.1", DB, "-c", f"TRUNCATE {tables} CASCADE"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PGPASSWORD": PGPASS}
    )

def get_state(session_id):
    sk = f"goose:{session_id}"
    sw = psql(f"SELECT cut, summarized_through, dropped_total FROM proxy.session_windows WHERE session_key='{sk}'")
    ps_count = psql(f"SELECT count(*) FROM proxy.phase_summaries WHERE task_id=(SELECT id FROM proxy.tasks WHERE session_id='{session_id}')")
    ps_max = psql(f"SELECT max(phase_number) FROM proxy.phase_summaries WHERE task_id=(SELECT id FROM proxy.tasks WHERE session_id='{session_id}')")
    return {"sw": sw, "ps_count": ps_count, "ps_max": ps_max}

def log_count(pattern):
    r = subprocess.run(
        f"grep -c '{pattern}' {SCRATCH}/sandbox_proxy.log 2>/dev/null",
        shell=True, capture_output=True, text=True
    )
    out = r.stdout.strip()
    return int(out) if out.isdigit() else 0

def log_lines(pattern):
    r = subprocess.run(
        f"grep '{pattern}' {SCRATCH}/sandbox_proxy.log 2>/dev/null",
        shell=True, capture_output=True, text=True
    )
    return r.stdout.strip()

def build_env(port=PROXY_PORT):
    """Build minimal env - NO os.environ inheritance."""
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": "/home/pawelw",
        "LANG": "en_US.UTF-8",
        "CTXGATE_SKIP_DOTENV": "1",
        "CTXGATE_PROXY_PORT": str(port),
        "CTXGATE_DB_DSN": f"postgresql://postgres:{PGPASS}@127.0.0.1:5432/{DB}",
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
        "CTXGATE_LM_API_KEY": "fake",
        "CTXGATE_LM_WORKERS": "2",
        "CTXGATE_QWEN_TOKENIZER": "/home/pawelw/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json",
    }

def start_fake_llm():
    proc = subprocess.Popen(
        [sys.executable, f"{SCRATCH}/fake_llm.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError("Fake LLM failed to start")
    return proc

def start_proxy(app_file):
    open(f"{SCRATCH}/sandbox_proxy.log", "w").close()
    proc = subprocess.Popen(
        [sys.executable, app_file],
        stdout=open(f"{SCRATCH}/sandbox_proxy.log", "a"),
        stderr=subprocess.STDOUT,
        env=build_env(),
        cwd="/home/pawelw/ctxproxy",
    )
    time.sleep(3)
    if proc.poll() is not None:
        log = open(f"{SCRATCH}/sandbox_proxy.log").read()
        raise RuntimeError(f"Proxy died: {log[-300:]}")
    # Debug: print budget config from log
    _log = open(f"{SCRATCH}/sandbox_proxy.log").read()
    for _l in _log.split("
"):
        if "Budget config" in _l:
            print(f"  [DEBUG] {_l}")
            break
    else:
        print(f"  [DEBUG] No Budget config line found! First 3 lines:")
        for _l in _log.split("
")[:3]:
            if _l.strip():
                print(f"    {_l}")
    return proc

def stop_proc(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

# Message content: each ~35 tokens, total of 3 user msgs = ~105 > MAX_INPUT(100)
# Each individual msg < ceiling(100) so no 413
MSG_A = "The quick brown fox jumps over the lazy dog while the cat watches from the tree branch above and the birds sing their morning song in the distant forest clearing"
MSG_B = "A large grey elephant walked slowly through the dense tropical jungle carrying a heavy wooden bundle on its back while the river flowed gently beside the ancient stone bridge"
MSG_C = "The old lighthouse keeper carefully tended his oil lamp every single evening as the ships passed safely along the rocky coastline and the stars began to appear in the darkening sky"
MSG_D = "The young student sat at her desk studying for the upcoming mathematics examination while the rain tapped softly against the window pane outside"

def over_limit_msgs():
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": MSG_A},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_B},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_C},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_D},
    ]

def over_limit_msgs_v2():
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": MSG_A},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_B},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_C},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_D},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "Please summarize everything we have discussed so far in detail"},
    ]


def main():
    results = {}
    print("=" * 60)
    print("AUX-ISOLATION VALIDATION v2")
    print("=" * 60)

    fake_llm = start_fake_llm()
    print(f"[Setup] Fake LLM PID: {fake_llm.pid}")

    try:
        # ===== SCENARIO A =====
        print("\n" + "=" * 60)
        print("SCENARIO A: Real trim + aux (FIXED)")
        print("=" * 60)
        truncate_db()
        proxy = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy PID: {proxy.pid}")

        sid = "scen-a"
        print("  [A1] Over-limit main request...")
        r = send_chat(sid, over_limit_msgs(), 30)
        time.sleep(1)
        s1 = get_state(sid)
        dropped = log_count("Dropped slice")
        seed_ch = log_count("SEED CHANGED")
        h3pop = log_count("H3-POP")
        print(f"  resp: {json.dumps(r)[:150]}")
        print(f"  state: {s1}")
        print(f"  dropped={dropped} seed_changed={seed_ch} h3pop={h3pop}")

        print("  [A2] 3 aux requests (2 msgs, diff system)...")
        for i in range(3):
            send_chat(sid, [
                {"role": "system", "content": f"Title gen {i+1}."},
                {"role": "user", "content": "Hi"},
            ], 15)
            time.sleep(0.5)
        s2 = get_state(sid)
        seed_ch2 = log_count("SEED CHANGED")
        h3pop2 = log_count("H3-POP")
        print(f"  state: {s2}")
        print(f"  seed_changed={seed_ch2} h3pop={h3pop2}")

        print("  [A3] Next over-limit main...")
        r3 = send_chat(sid, over_limit_msgs_v2(), 30)
        time.sleep(1)
        s3 = get_state(sid)
        seed_ch3 = log_count("SEED CHANGED")
        h3pop3 = log_count("H3-POP")
        print(f"  state: {s3}")
        print(f"  seed_changed={seed_ch3} h3pop={h3pop3}")

        # PASS: trim happened, no SEED CHANGED, no H3-POP
        a_pass = (dropped > 0) and (seed_ch3 == 0) and (h3pop3 == 0)
        results["A"] = {"pass": a_pass, "s1": s1, "s2": s2, "s3": s3,
                        "dropped": dropped, "seed_ch": seed_ch3, "h3pop": h3pop3}
        print(f"  => {'PASS' if a_pass else 'FAIL'}")
        stop_proc(proxy)

        # ===== SCENARIO B =====
        print("\n" + "=" * 60)
        print("SCENARIO B: Restart + resume (FIXED)")
        print("=" * 60)
        # Don't truncate - resume existing state
        proxy_b = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy B PID: {proxy_b.pid}")

        print("  [B1] Resume session...")
        send_chat(sid, over_limit_msgs_v2(), 30)
        time.sleep(1)
        s_b = get_state(sid)
        loaded = log_count("Loaded window")
        resum = log_count("resum")
        print(f"  state: {s_b}")
        print(f"  loaded_window={loaded} resum={resum}")

        # Check summarized_through didn't decrease
        b_pass = True
        if s1["sw"] and s_b["sw"]:
            parts1 = s1["sw"].split("|")
            parts_b = s_b["sw"].split("|")
            if len(parts1) >= 2 and len(parts_b) >= 2:
                try:
                    if int(parts_b[1]) < int(parts1[1]):
                        b_pass = False
                        print(f"  FAIL: summarized_through decreased")
                except ValueError:
                    pass
        results["B"] = {"pass": b_pass, "s_b": s_b, "loaded": loaded, "resum": resum}
        print(f"  => {'PASS' if b_pass else 'FAIL'}")
        stop_proc(proxy_b)

        # ===== SCENARIO C =====
        print("\n" + "=" * 60)
        print("SCENARIO C: Negative control (ORIGINAL)")
        print("=" * 60)
        truncate_db()
        proxy_c = start_proxy(f"{SCRATCH}/app_sandbox_orig.py")
        print(f"  Proxy C PID: {proxy_c.pid}")

        sid_c = "scen-c"
        print("  [C1] Over-limit main...")
        send_chat(sid_c, over_limit_msgs(), 30)
        time.sleep(1)
        s_c1 = get_state(sid_c)
        dropped_c = log_count("Dropped slice")
        print(f"  state: {s_c1} dropped={dropped_c}")

        print("  [C2] Aux request (triggers bug in original)...")
        send_chat(sid_c, [
            {"role": "system", "content": "Title gen."},
            {"role": "user", "content": "Hi"},
        ], 15)
        time.sleep(1)
        s_c2 = get_state(sid_c)
        h3pop_c = log_count("H3-POP")
        seed_c = log_count("SEED CHANGED")
        print(f"  state: {s_c2} h3pop={h3pop_c} seed_changed={seed_c}")

        print("  [C3] Next over-limit main...")
        send_chat(sid_c, over_limit_msgs_v2(), 30)
        time.sleep(1)
        s_c3 = get_state(sid_c)
        h3pop_c3 = log_count("H3-POP")
        seed_c3 = log_count("SEED CHANGED")
        print(f"  state: {s_c3} h3pop={h3pop_c3} seed_changed={seed_c3}")

        c_defect = (h3pop_c > 0) or (seed_c > 0)
        results["C"] = {"defect": c_defect, "s_c1": s_c1, "s_c2": s_c2, "s_c3": s_c3,
                       "h3pop": h3pop_c3, "seed": seed_c3, "dropped": dropped_c}
        print(f"  => Defect reproduced: {c_defect}")
        stop_proc(proxy_c)

        # ===== R1 =====
        print("\n" + "=" * 60)
        print("R1: Under-limit with stale state (FIXED)")
        print("=" * 60)
        truncate_db()
        proxy_r1 = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy R1 PID: {proxy_r1.pid}")

        sid_r1 = "r1-test"
        print("  [R1-1] Create over-limit state...")
        send_chat(sid_r1, over_limit_msgs(), 30)
        time.sleep(1)
        s_r1a = get_state(sid_r1)
        print(f"  state: {s_r1a}")

        print("  [R1-2] Short under-limit request (simulates compact)...")
        send_chat(sid_r1, [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hi"},
        ], 15)
        time.sleep(1)
        s_r1b = get_state(sid_r1)
        h3pop_r1 = log_count("H3-POP")
        print(f"  state: {s_r1b} h3pop={h3pop_r1}")

        print("  [R1-3] Over-limit again...")
        send_chat(sid_r1, over_limit_msgs_v2(), 30)
        time.sleep(1)
        s_r1c = get_state(sid_r1)
        h3pop_r1f = log_count("H3-POP")
        seed_r1f = log_count("SEED CHANGED")
        print(f"  state: {s_r1c} h3pop={h3pop_r1f} seed={seed_r1f}")

        r1_pass = (h3pop_r1f == 0) and (seed_r1f == 0)
        results["R1"] = {"pass": r1_pass, "h3pop": h3pop_r1f, "seed": seed_r1f}
        print(f"  => {'PASS' if r1_pass else 'FAIL'}")
        stop_proc(proxy_r1)

        # ===== R2 =====
        print("\n" + "=" * 60)
        print("R2: 3+ msgs, diff system prompt (FIXED)")
        print("=" * 60)
        truncate_db()
        proxy_r2 = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy R2 PID: {proxy_r2.pid}")

        sid_r2 = "r2-test"
        print("  [R2-1] Establish session...")
        send_chat(sid_r2, [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ], 20)
        time.sleep(1)

        print("  [R2-2] 4 msgs, different system...")
        send_chat(sid_r2, [
            {"role": "system", "content": "You are a title generator."},
            {"role": "user", "content": "Summarize"},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "Thanks"},
        ], 15)
        time.sleep(1)
        seed_r2 = log_count("SEED CHANGED")
        h3pop_r2 = log_count("H3-POP")
        print(f"  seed_changed={seed_r2} h3pop={h3pop_r2}")

        print("  [R2-3] Normal request...")
        send_chat(sid_r2, [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello again"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "How are you?"},
        ], 20)
        time.sleep(1)
        seed_r2f = log_count("SEED CHANGED")
        h3pop_r2f = log_count("H3-POP")
        print(f"  seed_changed={seed_r2f} h3pop={h3pop_r2f}")

        results["R2"] = {"corruption": h3pop_r2f > 0, "seed_flap": seed_r2f > 0,
                        "seed": seed_r2f, "h3pop": h3pop_r2f}
        print(f"  => Corruption={h3pop_r2f>0} SeedFlap={seed_r2f>0}")
        stop_proc(proxy_r2)

        # ===== R3 =====
        print("\n" + "=" * 60)
        print("R3: Prod events pollution (read-only)")
        print("=" * 60)
        r3 = psql("SELECT left(content,80), count(*) FROM proxy.events GROUP BY 1 ORDER BY 2 DESC LIMIT 20", db="ctxproxy")
        lines = [l for l in r3.split("\n") if l]
        title_like = [l for l in lines if "title" in l.lower() or "generate" in l.lower()]
        print(f"  Total distinct prefixes: {len(lines)}")
        print(f"  Title-like: {len(title_like)}")
        for l in lines[:5]:
            print(f"    {l}")
        results["R3"] = {"total": len(lines), "title_like": len(title_like)}

        # ===== PHASE 4 =====
        print("\n" + "=" * 60)
        print("PHASE 4: verify_aux_isolation.sh")
        print("=" * 60)
        truncate_db()
        proxy_v = start_proxy(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy V PID: {proxy_v.pid}")
        time.sleep(1)

        v_result = subprocess.run(
            ["bash", f"{SCRATCH}/verify_aux_isolation.sh"],
            capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "PGPASSWORD": PGPASS}
        )
        v_out = v_result.stdout
        v_pass = v_result.returncode == 0
        print(v_out[-500:] if len(v_out) > 500 else v_out)
        results["P4"] = {"pass": v_pass, "rc": v_result.returncode}
        print(f"  => {'PASS' if v_pass else 'FAIL'}")
        stop_proc(proxy_v)

    finally:
        stop_proc(fake_llm)

    # ===== SUMMARY =====
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for k in ["A", "B", "C", "R1", "R2", "R3", "P4"]:
        v = results.get(k, {})
        if "pass" in v:
            print(f"  {k}: {'PASS' if v['pass'] else 'FAIL'}")
        elif "defect" in v:
            print(f"  {k}: defect_reproduced={v['defect']}")
        elif "corruption" in v:
            print(f"  {k}: corruption={v['corruption']} seed_flap={v['seed_flap']}")
        elif "title_like" in v:
            print(f"  {k}: title_like={v['title_like']}/{v['total']}")

    with open(f"{SCRATCH}/test_results_v2.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results: {SCRATCH}/test_results_v2.json")

if __name__ == "__main__":
    main()

