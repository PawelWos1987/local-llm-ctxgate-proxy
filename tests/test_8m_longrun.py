
#!/usr/bin/env python3
import json, time, urllib.request, urllib.error, sys, os, resource

BASE = 'http://127.0.0.1:9200'
MODEL = 'Qwen3.8-27B'
SESSION = '8m-longrun-001'
TARGET_TOKENS = 8000000
MAX_INPUT_LIMIT = 64000

def http_post(path, body, headers=None):
    url = BASE + path
    hdrs = {'Content-Type': 'application/json'}
    if headers: hdrs.update(headers)
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=hdrs, method='POST')
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try: return e.code, json.loads(raw)
        except: return e.code, raw
    except Exception as e:
        return 0, str(e)

def get_metrics():
    try:
        resp = urllib.request.urlopen(BASE + '/metrics', timeout=10)
        return json.loads(resp.read().decode('utf-8'))
    except:
        return {}

def mem_mb():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss / 1024

def log(msg):
    ts = time.strftime('%H:%M:%S')
    print('[' + ts + '] ' + msg, flush=True)

def filler(n_words, seed):
    parts = []
    i = 0
    while len(' '.join(parts)) < n_words * 6:
        parts.append(seed + '_' + str(i))
        i += 1
    return ' '.join(parts)

print("=" * 70)
print("8M TOKEN LONG-RUN SIMULATED SESSION")
print("Target: 8,000,000 cumulative input tokens")
print("Proxy MAX_INPUT: 64,000 (trim cap)")
print("Session: " + SESSION)
print("Start: " + time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 70)

m0 = get_metrics()
ti0 = m0.get('tokens_in_total', 0)
rt0 = m0.get('requests_total', 0)
pi0 = m0.get('prefix_invalidations', 0)
re0 = m0.get('requests_error', 0)
to0 = m0.get('tokens_out_total', 0)
log('Baseline: tokens_in=' + str(ti0) + ' req_total=' + str(rt0) + ' prefix_inv=' + str(pi0))

# --- PHASE 1: Build context to ~64k ---
print("Phase 1: Building context to ~64k")
msgs = [{'role': 'system', 'content': 'You are a helpful assistant. Respond briefly.'}]
ctx_tokens = 0
pair_idx = 0
build_times = []
ASSIGN_CTX = 58000
PAIR_WORDS = 2000
while ctx_tokens < ASSIGN_CTX:
    t0 = time.time()
    user_text = filler(PAIR_WORDS, 'build' + str(pair_idx))
    msgs.append({'role': 'user', 'content': user_text})
    s, b = http_post('/v1/chat/completions', {
        'model': MODEL, 'messages': msgs,
        'max_tokens': 10, 'temperature': 0.1
    }, headers={'X-Session-ID': SESSION})
    dt = time.time() - t0
    build_times.append(dt)
    if s == 200:
        usage = b.get('usage', {})
        ti = usage.get('prompt_tokens', 0)
        to = usage.get('completion_tokens', 0)
        ctx_tokens = ti
        content = (b.get('choices', [{}])[0].get('message', {}).get('content') or '(ok)')[:50]
        msgs.append({'role': 'assistant', 'content': content})
        log('  Build ' + str(pair_idx).rjust(3) + ': ' + str(round(dt,2)) + 's ctx=' + str(ti) + ' (+ ' + str(to) + ' out)')
    else:
        log('  Build ' + str(pair_idx).rjust(3) + ': ERROR ' + str(s))
        break
    pair_idx += 1
    if pair_idx >= 80: break

avg_build = sum(build_times) / len(build_times) if build_times else 0
print("Phase 1 done: " + str(pair_idx) + " pairs, avg " + str(round(avg_build,2)) + "s/req, ctx=" + str(ctx_tokens))

# --- PHASE 2: Rapid-fire at 64k cap ---
print("Phase 2: Rapid-fire at ~64k context (trim active)")
m1 = get_metrics()
rapid_times = []
rapid_req_idx = 0
mem_start = mem_mb()
CONSEC_504 = 0
while True:
    m_curr = get_metrics()
    ti_curr = m_curr.get('tokens_in_total', 0)
    delta = ti_curr - ti0
    pct = delta / TARGET_TOKENS * 100
    log('  Progress: ' + str(delta) + ' / ' + str(TARGET_TOKENS) + ' (' + str(round(pct,1)) + '%)')
    if delta >= TARGET_TOKENS:
        log('  TARGET REACHED!')
        break
    t0 = time.time()
    user_text = 'Rapid ' + str(rapid_req_idx) + ': ' + filler(100, 'rf' + str(rapid_req_idx))
    msgs.append({'role': 'user', 'content': user_text})
    s, b = http_post('/v1/chat/completions', {
        'model': MODEL, 'messages': msgs,
        'max_tokens': 10, 'temperature': 0.1
    }, headers={'X-Session-ID': SESSION})
    dt = time.time() - t0
    rapid_times.append(dt)
    if s == 200:
        usage = b.get('usage', {})
        ti = usage.get('prompt_tokens', 0)
        to = usage.get('completion_tokens', 0)
        content = (b.get('choices', [{}])[0].get('message', {}).get('content') or '(ok)')[:30]
        msgs.append({'role': 'assistant', 'content': content})
        if rapid_req_idx % 10 == 0:
            log('  R' + str(rapid_req_idx).rjust(3) + ': ' + str(round(dt,2)) + 's ctx=' + str(ti) + ' (+ ' + str(to) + ') msgs=' + str(len(msgs)))
        CONSEC_504 = 0
    else:
        log('  R' + str(rapid_req_idx).rjust(3) + ': ERROR ' + str(s))
        if s in (500, 502, 503, 504): CONSEC_504 += 1
        else: CONSEC_504 = 0
    rapid_req_idx += 1
    if CONSEC_504 >= 5:
        log('  Too many 5xx errors, stopping')
        break
    if rapid_req_idx >= 300:
        log('  Safety cap 300 reached')
        break

# --- PHASE 3: Results ---
m_final = get_metrics()
ti_final = m_final.get('tokens_in_total', 0)
rt_final = m_final.get('requests_total', 0)
to_final = m_final.get('tokens_out_total', 0)
pi_final = m_final.get('prefix_invalidations', 0)
re_final = m_final.get('requests_error', 0)
mem_end = mem_mb()

total_in = ti_final - ti0
total_out = to_final - to0
total_reqs = rt_final - rt0
total_pi = pi_final - pi0
total_re = re_final - re0
all_times = build_times + rapid_times
avg_t = sum(all_times) / len(all_times) if all_times else 0
max_t = max(all_times) if all_times else 0
min_t = min(all_times) if all_times else 0

print("")
print("=" * 70)
print("8M LONG-RUN RESULTS")
print("=" * 70)
print("Total input tokens: " + str(total_in))
print("Target: " + str(TARGET_TOKENS))
print("Target reached: " + str(total_in >= TARGET_TOKENS))
print("Total output tokens: " + str(total_out))
print("Total requests: " + str(total_reqs) + " (build=" + str(pair_idx) + " rapid=" + str(rapid_req_idx) + ")")
print("Prefix invalidations: " + str(total_pi))
print("Error requests: " + str(total_re))
print("Latency avg: " + str(round(avg_t,2)) + "s  min: " + str(round(min_t,2)) + "s  max: " + str(round(max_t,2)) + "s")
print("Proxy mem: " + str(round(mem_start)) + "MB -> " + str(round(mem_end)) + "MB (delta " + str(round(mem_end-mem_start)) + "MB)")
print("Final context: ~" + str(ctx_tokens) + " tokens, " + str(len(msgs)) + " messages")
print("=" * 70)

results = {
    'total_input_tokens': total_in,
    'total_output_tokens': total_out,
    'target': TARGET_TOKENS,
    'target_reached': total_in >= TARGET_TOKENS,
    'total_requests': total_reqs,
    'build_requests': pair_idx,
    'rapid_requests': rapid_req_idx,
    'prefix_invalidations': total_pi,
    'error_requests': total_re,
    'latency_avg_s': round(avg_t, 2),
    'latency_min_s': round(min_t, 2),
    'latency_max_s': round(max_t, 2),
    'proxy_mem_start_mb': round(mem_start, 1),
    'proxy_mem_end_mb': round(mem_end, 1),
    'final_context_tokens': ctx_tokens,
    'final_message_count': len(msgs)
}
with open('/tmp/8m_results.json', 'w') as f:
    json.dump(results, f, indent=2)
print("Results saved to /tmp/8m_results.json")
