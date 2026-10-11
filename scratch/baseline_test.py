#!/usr/bin/env python3
"""Baseline measurement for 8f34efd (exact tokenizer)."""
import json, time, httpx, threading, sys, os

PROXY = "http://127.0.0.1:19201"
CAPTURE = "/tmp/session_capture.json"

def main():
    # Load capture
    with open(CAPTURE) as f:
        cap = json.load(f)
    
    messages = cap["messages"]
    tools = cap.get("tools")
    total_chars = sum(len(m.get("content") or "") for m in messages)
    print(f"Capture: {len(messages)} messages, {total_chars} chars")
    
    body = {
        "model": "Qwen3.8-27B",
        "messages": messages,
        "max_tokens": 16000,
        "temperature": 0,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
    
    # Event loop probe
    max_gap = [0.0]
    stop_probe = [False]
    last_tick = [time.monotonic()]
    
    def probe():
        while not stop_probe[0]:
            now = time.monotonic()
            gap = (now - last_tick[0]) * 1000
            if gap > max_gap[0]:
                max_gap[0] = gap
            last_tick[0] = now
            time.sleep(0.01)
    
    probe_thread = threading.Thread(target=probe, daemon=True)
    probe_thread.start()
    
    # COLD request
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{PROXY}/v1/chat/completions", json=body, timeout=120)
        t_cold = (time.monotonic() - t0) * 1000
        data = r.json()
        usage = data.get("usage", {})
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"COLD: {t_cold:.0f}ms status={r.status_code} prompt_tokens={usage.get('prompt_tokens')} content_len={len(content)}")
    except Exception as e:
        t_cold = (time.monotonic() - t0) * 1000
        print(f"COLD: {t_cold:.0f}ms ERROR: {e}")
    
    # WARM: 5 requests, same session
    warm_times = []
    for i in range(5):
        t0 = time.monotonic()
        try:
            r = httpx.post(f"{PROXY}/v1/chat/completions", json=body, timeout=120)
            t_warm = (time.monotonic() - t0) * 1000
            warm_times.append(t_warm)
            data = r.json()
            usage = data.get("usage", {})
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"WARM{i+1}: {t_warm:.0f}ms status={r.status_code} prompt_tokens={usage.get('prompt_tokens')} content_len={len(content)}")
        except Exception as e:
            t_warm = (time.monotonic() - t0) * 1000
            warm_times.append(t_warm)
            print(f"WARM{i+1}: {t_warm:.0f}ms ERROR: {e}")
    
    stop_probe[0] = True
    probe_thread.join(timeout=2)
    
    print(f"\nMAX_EVENT_LOOP_GAP: {max_gap[0]:.1f}ms")
    print(f"WARM_AVG: {sum(warm_times)/len(warm_times):.0f}ms")
    print(f"WARM_MIN: {min(warm_times):.0f}ms")

if __name__ == "__main__":
    main()
