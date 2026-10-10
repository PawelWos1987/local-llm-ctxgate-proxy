#!/usr/bin/env python3
"""AUX-ISOLATION VALIDATION v3 - clean rewrite"""
import json, os, subprocess, sys, time, urllib.request, urllib.error

SCRATCH = "/home/pawelw/ctxproxy/scratch"
PORT = 19203
FAKE = 19204
DB = "ctxproxy_sandbox"

def pgpass():
    return subprocess.check_output(
        "grep '^CTXGATE_PG_PASS=' /home/pawelw/ctxproxy/.env | cut -d= -f2",
        shell=True, text=True
    ).strip()

PASS = pgpass()

def psql(q, db=DB):
    r = subprocess.run(
        ["psql", "-U", "postgres", "-h", "127.0.0.1", db, "-t", "-A", "-c", q],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PGPASSWORD": PASS}
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

def chat(sid, msgs, mt=30):
    return http_post(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        {"model": "Qwen3.8-27B", "messages": msgs, "max_tokens": mt, "stream": False},
        {"agent-session-id": sid}
    )

def truncate():
    subprocess.run(
        ["psql", "-U", "postgres", "-h", "127.0.0.1", DB, "-c",
         "TRUNCATE proxy.tasks, proxy.session_windows, proxy.events, proxy.memory_jobs, "
         "proxy.knowledge, proxy.memories, proxy.phase_summaries, proxy.session_summaries, "
         "proxy.working_memory, proxy.session_ledger, proxy.deliverables CASCADE"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PGPASSWORD": PASS}
    )

def state(sid):
    sk = f"goose:{sid}"
    sw = psql(f"SELECT cut, summarized_through, dropped_total FROM proxy.session_windows WHERE session_key='{sk}'")
    pc = psql(f"SELECT count(*) FROM proxy.phase_summaries WHERE task_id=(SELECT id FROM proxy.tasks WHERE session_id='{sid}')")
    pm = psql(f"SELECT max(phase_number) FROM proxy.phase_summaries WHERE task_id=(SELECT id FROM proxy.tasks WHERE session_id='{sid}')")
    return {"sw": sw, "ps": pc, "pm": pm}

def lc(pat):
    r = subprocess.run(f"grep -c '{pat}' {SCRATCH}/sandbox_proxy.log 2>/dev/null",
                       shell=True, capture_output=True, text=True)
    return int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0

def env():
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": "/home/pawelw",
        "LANG": "en_US.UTF-8",
        "CTXGATE_SKIP_DOTENV": "1",
        "CTXGATE_COMPACTION_NOTE": "",
        "CTXGATE_PROXY_PORT": str(PORT),
        "CTXGATE_DB_DSN": f"postgresql://postgres:{PASS}@127.0.0.1:5432/{DB}",
        "CTXGATE_ALLOW_NO_AUTH": "1",
        "CTXGATE_HOST": "127.0.0.1",
        "CTXGATE_MAX_CONTEXT": "200",
        "CTXGATE_MAX_INPUT": "100",
        "CTXGATE_MAX_OUTPUT": "50",
        "CTXGATE_SAFETY_MARGIN": "20",
        "CTXGATE_MIN_OUTPUT": "30",
        "CTXGATE_VLLM_URL": f"http://127.0.0.1:{FAKE}/v1",
        "CTXGATE_VLLM_MODEL": "Qwen3.8-27B",
        "CTXGATE_LM_URL": f"http://127.0.0.1:{FAKE}/v1/chat/completions",
        "CTXGATE_LM_MODEL": "fake-llm",
        "CTXGATE_LM_API_KEY": "fake",
        "CTXGATE_LM_WORKERS": "2",
        "CTXGATE_QWEN_TOKENIZER": "/home/pawelw/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json",
    }

def start(app):
    open(f"{SCRATCH}/sandbox_proxy.log", "w").close()
    p = subprocess.Popen(
        [sys.executable, app],
        stdout=open(f"{SCRATCH}/sandbox_proxy.log", "a"),
        stderr=subprocess.STDOUT,
        env=env(),
        cwd="/home/pawelw/ctxproxy",
    )
    time.sleep(3)
    if p.poll() is not None:
        log = open(f"{SCRATCH}/sandbox_proxy.log").read()
        raise RuntimeError(f"Proxy died: {log[-300:]}")
    return p

def stop(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

# Messages: each ~30 tokens, 4 user msgs = ~124 total > 100
MA = "The weather in Paris has been unusually warm this week with temperatures consistently reaching well above the seasonal average of fifteen degrees celsius every single day"
MB = "The stock market showed significant and sustained gains across the entire technology sector during this particularly volatile and unpredictable morning trading session on Wall Street today"
MC = "The new groundbreaking research paper on quantum computing breakthroughs has been published in the prestigious peer-reviewed journal Nature and received widespread international attention this morning"
MD = "The local professional football team dramatically won their championship game last night after an incredibly exciting and heart-stopping final minute of intense play on the field"

def over1():
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": MA},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MB},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MC},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MD},
    ]

def over2():
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": MA},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MB},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MC},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MD},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "Please summarize everything we have discussed so far in detail"},
    ]

def main():
    R = {}
    print("=" * 60)
    print("AUX-ISOLATION VALIDATION v3")
    print("=" * 60)

    # Start fake LLM
    fl = subprocess.Popen([sys.executable, f"{SCRATCH}/fake_llm.py"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    print(f"[Setup] Fake LLM PID: {fl.pid}")

    try:
        # === SCENARIO A ===
        print("\n" + "=" * 60)
        print("SCENARIO A: Real trim + aux (FIXED)")
        print("=" * 60)
        truncate()
        px = start(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy PID: {px.pid}")

        sid = "scen-a"
        print("  [A1] Over-limit main...")
        r = chat(sid, over1(), 30)
        time.sleep(1)
        s1 = state(sid)
        d1 = lc("After trim")
        sc1 = lc("SEED CHANGED")
        hp1 = lc("H3-POP")
        print(f"  resp: {json.dumps(r)[:120]}")
        print(f"  state: {s1} dropped={d1} seed={sc1} h3pop={hp1}")

        print("  [A2] 3 aux requests...")
        for i in range(3):
            chat(sid, [{"role": "system", "content": f"Title gen {i+1}."},
                       {"role": "user", "content": "Hi"}], 15)
            time.sleep(0.5)
        s2 = state(sid)
        sc2 = lc("SEED CHANGED")
        hp2 = lc("H3-POP")
        print(f"  state: {s2} seed={sc2} h3pop={hp2}")

        print("  [A3] Next over-limit main...")
        chat(sid, over2(), 30)
        time.sleep(1)
        s3 = state(sid)
        sc3 = lc("SEED CHANGED")
        hp3 = lc("H3-POP")
        print(f"  state: {s3} seed={sc3} h3pop={hp3}")

        a_ok = d1 > 0 and sc3 == 0 and hp3 == 0
        R["A"] = {"pass": a_ok, "s1": s1, "s3": s3, "dropped": d1, "seed": sc3, "h3pop": hp3}
        print(f"  => {'PASS' if a_ok else 'FAIL'}")
        stop(px)

        # === SCENARIO B ===
        print("\n" + "=" * 60)
        print("SCENARIO B: Restart + resume (FIXED)")
        print("=" * 60)
        pxb = start(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy B PID: {pxb.pid}")
        print("  [B1] Resume...")
        chat(sid, over2(), 30)
        time.sleep(1)
        sb = state(sid)
        lw = lc("Loaded window")
        rs = lc("resum")
        print(f"  state: {sb} loaded={lw} resum={rs}")
        b_ok = True
        if s1["sw"] and sb["sw"]:
            p1 = s1["sw"].split("|")
            pb = sb["sw"].split("|")
            if len(p1) >= 2 and len(pb) >= 2:
                try:
                    if int(pb[1]) < int(p1[1]):
                        b_ok = False
                except ValueError:
                    pass
        R["B"] = {"pass": b_ok, "sb": sb, "loaded": lw, "resum": rs}
        print(f"  => {'PASS' if b_ok else 'FAIL'}")
        stop(pxb)

        # === SCENARIO C ===
        print("\n" + "=" * 60)
        print("SCENARIO C: Negative control (ORIGINAL)")
        print("=" * 60)
        truncate()
        pxc = start(f"{SCRATCH}/app_sandbox_orig.py")
        print(f"  Proxy C PID: {pxc.pid}")
        sidc = "scen-c"
        print("  [C1] Over-limit main...")
        chat(sidc, over1(), 30)
        time.sleep(1)
        sc1s = state(sidc)
        dc = lc("After trim")
        print(f"  state: {sc1s} dropped={dc}")

        print("  [C2] Aux (triggers bug)...")
        chat(sidc, [{"role": "system", "content": "Title gen."},
                    {"role": "user", "content": "Hi"}], 15)
        time.sleep(1)
        sc2s = state(sidc)
        hpc = lc("H3-POP")
        scc = lc("SEED CHANGED")
        print(f"  state: {sc2s} h3pop={hpc} seed={scc}")

        print("  [C3] Next over-limit...")
        chat(sidc, over2(), 30)
        time.sleep(1)
        sc3s = state(sidc)
        hpc3 = lc("H3-POP")
        scc3 = lc("SEED CHANGED")
        print(f"  state: {sc3s} h3pop={hpc3} seed={scc3}")

        c_def = hpc > 0 or scc > 0
        R["C"] = {"defect": c_def, "sc1": sc1s, "sc3": sc3s, "h3pop": hpc3, "seed": scc3}
        print(f"  => Defect: {c_def}")
        stop(pxc)

        # === R1 ===
        print("\n" + "=" * 60)
        print("R1: Under-limit stale state (FIXED)")
        print("=" * 60)
        truncate()
        pxr1 = start(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy R1 PID: {pxr1.pid}")
        sidr1 = "r1"
        print("  [R1-1] Create over-limit...")
        chat(sidr1, over1(), 30)
        time.sleep(1)
        sr1a = state(sidr1)
        print(f"  state: {sr1a}")

        print("  [R1-2] Short under-limit (compact sim)...")
        chat(sidr1, [{"role": "system", "content": "You are a helpful assistant."},
                     {"role": "user", "content": "Hi"}], 15)
        time.sleep(1)
        sr1b = state(sidr1)
        hpr1 = lc("H3-POP")
        print(f"  state: {sr1b} h3pop={hpr1}")

        print("  [R1-3] Over-limit again...")
        chat(sidr1, over2(), 30)
        time.sleep(1)
        sr1c = state(sidr1)
        hpr1f = lc("H3-POP")
        scr1f = lc("SEED CHANGED")
        print(f"  state: {sr1c} h3pop={hpr1f} seed={scr1f}")

        r1ok = hpr1f == 0 and scr1f == 0
        R["R1"] = {"pass": r1ok, "h3pop": hpr1f, "seed": scr1f}
        print(f"  => {'PASS' if r1ok else 'FAIL'}")
        stop(pxr1)

        # === R2 ===
        print("\n" + "=" * 60)
        print("R2: 3+ msgs diff system (FIXED)")
        print("=" * 60)
        truncate()
        pxr2 = start(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy R2 PID: {pxr2.pid}")
        sidr2 = "r2"
        print("  [R2-1] Establish...")
        chat(sidr2, [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ], 20)
        time.sleep(1)

        print("  [R2-2] 4 msgs diff system...")
        chat(sidr2, [
            {"role": "system", "content": "You are a title generator."},
            {"role": "user", "content": "Summarize"},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "Thanks"},
        ], 15)
        time.sleep(1)
        scr2 = lc("SEED CHANGED")
        hpr2 = lc("H3-POP")
        print(f"  seed={scr2} h3pop={hpr2}")

        print("  [R2-3] Normal...")
        chat(sidr2, [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello again"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "How are you?"},
        ], 20)
        time.sleep(1)
        scr2f = lc("SEED CHANGED")
        hpr2f = lc("H3-POP")
        print(f"  seed={scr2f} h3pop={hpr2f}")

        R["R2"] = {"corrupt": hpr2f > 0, "flap": scr2f > 0, "seed": scr2f, "h3pop": hpr2f}
        print(f"  => Corrupt={hpr2f>0} Flap={scr2f>0}")
        stop(pxr2)

        # === R3 ===
        print("\n" + "=" * 60)
        print("R3: Prod events (read-only)")
        print("=" * 60)
        r3 = psql("SELECT left(content,80), count(*) FROM proxy.events GROUP BY 1 ORDER BY 2 DESC LIMIT 20", db="ctxproxy")
        lines = [l for l in r3.split("\n") if l]
        tl = [l for l in lines if "title" in l.lower() or "generate" in l.lower()]
        print(f"  Total: {len(lines)} Title-like: {len(tl)}")
        for l in lines[:5]:
            print(f"    {l}")
        R["R3"] = {"total": len(lines), "title_like": len(tl)}

        # === PHASE 4 ===
        print("\n" + "=" * 60)
        print("PHASE 4: verify_aux_isolation.sh")
        print("=" * 60)
        truncate()
        pxv = start(f"{SCRATCH}/app_sandbox.py")
        print(f"  Proxy V PID: {pxv.pid}")
        time.sleep(1)
        vr = subprocess.run(
            ["bash", f"{SCRATCH}/verify_aux_isolation.sh"],
            capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "PGPASSWORD": PASS}
        )
        v_ok = vr.returncode == 0
        out = vr.stdout
        print(out[-400:] if len(out) > 400 else out)
        R["P4"] = {"pass": v_ok, "rc": vr.returncode}
        print(f"  => {'PASS' if v_ok else 'FAIL'}")
        stop(pxv)

    finally:
        stop(fl)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for k in ["A", "B", "C", "R1", "R2", "R3", "P4"]:
        v = R.get(k, {})
        if "pass" in v:
            print(f"  {k}: {'PASS' if v['pass'] else 'FAIL'}")
        elif "defect" in v:
            print(f"  {k}: defect={v['defect']}")
        elif "corrupt" in v:
            print(f"  {k}: corrupt={v['corrupt']} flap={v['flap']}")
        elif "title_like" in v:
            print(f"  {k}: title={v['title_like']}/{v['total']}")

    with open(f"{SCRATCH}/test_results_v3.json", "w") as f:
        json.dump(R, f, indent=2, default=str)
    print(f"\n  Results: {SCRATCH}/test_results_v3.json")

if __name__ == "__main__":
    main()
