#!/usr/bin/env python3
"""Phase 2: Reproduce per-session state corruption.
With MAX_INPUT=100, we need:
- Main request: >100 tokens (triggers trim)
- Aux request: <100 tokens (triggers H3-POP)
- Main request again: >100 tokens (should reload from DB, but _window_load_tried blocks it)
"""
import asyncio, json, time, subprocess
import httpx

PROXY = "http://127.0.0.1:19203"
PGPASS = subprocess.run("grep '^CTXGATE_PG_PASS=' /home/pawelw/ctxproxy/.env | cut -d= -f2", 
                        shell=True, capture_output=True, text=True).stdout.strip()

def dbq(sql):
    cmd = f'PGPASSWORD={PGPASS} psql -U postgres -h 127.0.0.1 ctxproxy_sandbox -t -A -c "{sql}"'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip()

async def send(session_id, messages, label="", max_tokens=30):
    body = {"model": "Qwen3.8-27B", "messages": messages, "max_tokens": max_tokens, "stream": False}
    headers = {"Content-Type": "application/json", "agent-session-id": session_id}
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=120) as c:
        resp = await c.post(PROXY + "/v1/chat/completions", json=body, headers=headers)
    dt = time.monotonic() - t0
    print(f"  [{label}] status={resp.status_code} dt={dt:.1f}s")
    return resp

def state(sid, sk):
    tid = dbq(f"SELECT id FROM proxy.tasks WHERE session_id='{sid}'")
    if not tid: return "NO_TASK"
    sw = dbq(f"SELECT cut||','||summarized_through||','||dropped_total FROM proxy.session_windows WHERE session_key='{sk}'")
    ps = dbq(f"SELECT count(*)||'/max='||coalesce(max(phase_number),0) FROM proxy.phase_summaries WHERE task_id='{tid}'")
    ev = dbq(f"SELECT count(*) FROM proxy.events WHERE task_id='{tid}'")
    mj = dbq(f"SELECT count(*) FROM proxy.memory_jobs WHERE task_id='{tid}'")
    return f"sw=[{sw}] ps={ps} ev={ev} mj={mj}"

def log_grep(pattern, n=20):
    r = subprocess.run(f"grep -E '{pattern}' /home/pawelw/ctxproxy/scratch/sandbox_proxy.log | tail -{n}", 
                       shell=True, capture_output=True, text=True).stdout
    return [l for l in r.strip().split("\n") if l]

async def main():
    SID = "p2-clean"
    SK = "goose:" + SID
    
    print("=== PHASE 2: Clean run with MAX_INPUT=100 ===")
    
    # Content that is ~120 tokens (over 100 limit)
    # "The quick brown fox jumps over the lazy dog" is ~10 tokens
    # 12 sentences = ~120 tokens
    over_content = "The quick brown fox jumps over the lazy dog. " * 12
    # Content that is ~30 tokens (under 100 limit)
    under_content = "The quick brown fox jumps."
    
    # === STEP 1: Main request (over limit, triggers trim) ===
    print("\n--- STEP 1: Main request (>100 tokens, triggers trim) ---")
    main_msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": over_content},
    ]
    resp = await send(SID, main_msgs, label="main-1-over")
    
    logs = log_grep(r"Context:|Dropped|re-cut|slow.path|SET slow|H3-POP|H2-|SEED|_window_load_tried")
    print("  Log:")
    for l in logs: print(f"    {l}")
    print(f"  DB: {state(SID, SK)}")
    
    # === STEP 2: Auxiliary request (under limit, triggers H3-POP) ===
    print("\n--- STEP 2: Auxiliary request (<100 tokens, triggers H3-POP) ---")
    aux_msgs = [
        {"role": "system", "content": "Generate a short title for this conversation."},
        {"role": "user", "content": under_content},
    ]
    resp = await send(SID, aux_msgs, label="aux-title", max_tokens=15)
    
    logs = log_grep(r"H3-POP|H2-|SEED CHANGED|Context:")
    print("  Log:")
    for l in logs: print(f"    {l}")
    print(f"  DB: {state(SID, SK)}")
    
    # === STEP 3: Main request again (over limit, should reload from DB) ===
    print("\n--- STEP 3: Main request again (>100 tokens, should reload) ---")
    main_msgs2 = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": over_content},
        {"role": "assistant", "content": "Here is my response about the fox."},
        {"role": "user", "content": over_content},
    ]
    resp = await send(SID, main_msgs2, label="main-2-over")
    
    logs = log_grep(r"Context:|Dropped|re-cut|slow.path|SET slow|SET from DB|H3-POP|H2-|SEED|_window_load_tried|st_start")
    print("  Log:")
    for l in logs: print(f"    {l}")
    print(f"  DB: {state(SID, SK)}")
    
    # === VERDICT ===
    print("\n" + "="*60)
    print("VERDICT")
    print("="*60)
    all_logs = subprocess.run("grep -E 'H3-POP|H2-POP|H2-SEED-FLAP|SEED CHANGED|SET from DB|SET slow|_window_load_tried|slow.path' /home/pawelw/ctxproxy/scratch/sandbox_proxy.log", 
                             shell=True, capture_output=True, text=True).stdout
    print("\nAll relevant log lines:")
    for l in all_logs.strip().split("\n"):
        if l: print(f"  {l}")
    
    h3_fired = "H3-POP" in all_logs
    h2_fired = "H2-POP" in all_logs or "H2-SEED-FLAP" in all_logs
    db_reload = "SET from DB" in all_logs
    slow_path = "slow.path" in all_logs
    
    print(f"\n  H3-POP fired: {h3_fired}")
    print(f"  H2-POP/SEED-FLAP fired: {h2_fired}")
    print(f"  DB reload (SET from DB): {db_reload}")
    print(f"  Slow path taken: {slow_path}")
    
    if h3_fired and not db_reload:
        print("\n  *** DEFECT CONFIRMED: H3-POP wiped state, no DB reload on next request ***")
    elif h3_fired and db_reload:
        print("\n  H3-POP fired but DB reload recovered (less severe)")
    else:
        print("\n  No defect observed")

asyncio.run(main())
