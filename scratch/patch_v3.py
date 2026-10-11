#!/usr/bin/env python3
"""Second patch: calibrator hooks, K3/K4/K7/K10 fixes, telemetry, metrics."""
import sys, ast

PATH = "/home/pawelw/ctxproxy/proxy/app.py"

with open(PATH) as f:
    src = f.read()

original_lines = src.count("\n") + 1
print(f"Original: {original_lines} lines")

# ============================================================
# 1. Remove _smart_messages_tokens call (line 4053)
# ============================================================
old_smart_call = """        input_tokens = _smart_messages_tokens(
            built, _sensitive_input_lo, _sensitive_input_hi)"""
new_smart_call = """        input_tokens = count_messages_tokens(built)"""
assert old_smart_call in src, "smart_messages_tokens call not found"
src = src.replace(old_smart_call, new_smart_call, 1)
print("1. Replaced _smart_messages_tokens call")

# ============================================================
# 2. K10 + Calibrator hook: Non-stream path (line 4418 area)
#    Capture real BEFORE overwrite, call observe, then forward real (not estimate)
# ============================================================
old_ns_usage = """        out_tok = data["usage"].get("completion_tokens", 0)
        cached = (data["usage"].get(
            "prompt_tokens_details") or {}).get("cached_tokens", 0)
        data["usage"]["prompt_tokens"] = input_tokens
        data["usage"]["total_tokens"] = input_tokens + out_tok"""
new_ns_usage = """        out_tok = data["usage"].get("completion_tokens", 0)
        cached = (data["usage"].get(
            "prompt_tokens_details") or {}).get("cached_tokens", 0)
        _real_pt = data["usage"].get("prompt_tokens", 0)
        if _real_pt > 0:
            calibrator.observe(session_key, input_tokens, _real_pt)
            log.info("TOKEST est=%d real=%d err=%+.1f%% ratio=%.3f m=%.3f session=%s",
                     input_tokens, _real_pt, 100.0*(input_tokens-_real_pt)/_real_pt,
                     calibrator.ratio, calibrator.get_rho(session_key)[1], session_key)
        # K10: forward vLLM's real prompt_tokens (not the estimate)
        data["usage"]["prompt_tokens"] = _real_pt if _real_pt > 0 else input_tokens
        data["usage"]["total_tokens"] = data["usage"]["prompt_tokens"] + out_tok"""
assert old_ns_usage in src, "non-stream usage block not found"
src = src.replace(old_ns_usage, new_ns_usage, 1)
print("2. Added calibrator hook + K10 to non-stream path")

# ============================================================
# 3. Calibrator hook: Stream path (line 5054 area)
# ============================================================
old_stream_usage = """                                    total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                    usage["prompt_tokens"] = input_tokens
                                    usage["total_tokens"] = input_tokens + (usage.get("completion_tokens") or 0)"""
new_stream_usage = """                                    total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                    _real_pt_s = usage.get("prompt_tokens", 0)
                                    if _real_pt_s > 0:
                                        calibrator.observe(session_key, input_tokens, _real_pt_s)
                                        log.info("TOKEST est=%d real=%d err=%+.1f%% ratio=%.3f m=%.3f session=%s",
                                                 input_tokens, _real_pt_s, 100.0*(input_tokens-_real_pt_s)/_real_pt_s,
                                                 calibrator.ratio, calibrator.get_rho(session_key)[1], session_key)
                                    usage["prompt_tokens"] = _real_pt_s if _real_pt_s > 0 else input_tokens
                                    usage["total_tokens"] = usage["prompt_tokens"] + (usage.get("completion_tokens") or 0)"""
assert old_stream_usage in src, "stream usage block (first) not found"
src = src.replace(old_stream_usage, new_stream_usage, 1)
print("3. Added calibrator hook to stream path")

# ============================================================
# 4. Calibrator hook: Stream retry path (line 5391 area)
# ============================================================
old_retry_usage = """                                        total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                        usage["prompt_tokens"] = input_tokens
                                        usage["total_tokens"] = input_tokens + (usage.get("completion_tokens") or 0)
                                        try:
                                            _ptd = usage.get("prompt_tokens_details") or {}
                                            _ct = _ptd.get("cached_tokens", 0)
                                            _pt = usage.get("prompt_tokens", 0)
                                            if _pt:
                                                log.info("Retry cache: cached=%d/%d prompt_tokens (%.0f%%)", _ct, _pt, 100.0 * _ct / _pt)
                                        except Exception:
                                            pass"""
new_retry_usage = """                                        total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                        _real_pt_r = usage.get("prompt_tokens", 0)
                                        if _real_pt_r > 0:
                                            calibrator.observe(session_key, input_tokens, _real_pt_r)
                                            log.info("TOKEST est=%d real=%d err=%+.1f%% ratio=%.3f m=%.3f session=%s",
                                                     input_tokens, _real_pt_r, 100.0*(input_tokens-_real_pt_r)/_real_pt_r,
                                                     calibrator.ratio, calibrator.get_rho(session_key)[1], session_key)
                                        usage["prompt_tokens"] = _real_pt_r if _real_pt_r > 0 else input_tokens
                                        usage["total_tokens"] = usage["prompt_tokens"] + (usage.get("completion_tokens") or 0)
                                        try:
                                            _ptd = usage.get("prompt_tokens_details") or {}
                                            _ct = _ptd.get("cached_tokens", 0)
                                            _pt = usage.get("prompt_tokens", 0)
                                            if _pt:
                                                log.info("Retry cache: cached=%d/%d prompt_tokens (%.0f%%)", _ct, _pt, 100.0 * _ct / _pt)
                                        except Exception:
                                            pass"""
assert old_retry_usage in src, "stream retry usage block not found"
src = src.replace(old_retry_usage, new_retry_usage, 1)
print("4. Added calibrator hook to stream retry path")

# ============================================================
# 5. K7: Overflow feedback - bump margin on 400 (non-stream)
# ============================================================
old_400_ns = """            if _is_ctx400:
                log.warning("vLLM 400 (context/max_tokens) - re-shrinking and retrying: %s", resp.text[:200])
                reduced_limit = int(input_tokens * 0.8)"""
new_400_ns = """            if _is_ctx400:
                log.warning("vLLM 400 (context/max_tokens) - re-shrinking and retrying: %s", resp.text[:200])
                calibrator.bump_margin(session_key)
                reduced_limit = int(input_tokens * 0.8)"""
assert old_400_ns in src, "non-stream 400 block not found"
src = src.replace(old_400_ns, new_400_ns, 1)
print("5. Added bump_margin to non-stream 400")

# ============================================================
# 6. K7: Overflow feedback - bump margin on 400 (stream)
# ============================================================
old_400_stream = """                                shrink_retried = True
                                log.warning("Stream 400 (context/max_tokens) - re-shrinking and retrying: %s", _et[:200])
                                try:
                                    current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))"""
new_400_stream = """                                shrink_retried = True
                                log.warning("Stream 400 (context/max_tokens) - re-shrinking and retrying: %s", _et[:200])
                                calibrator.bump_margin(session_key)
                                try:
                                    current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))"""
assert old_400_stream in src, "stream 400 block not found"
src = src.replace(old_400_stream, new_400_stream, 1)
print("6. Added bump_margin to stream 400")

# ============================================================
# 7. K7: Overflow feedback - bump margin on 400 (stream retry)
# ============================================================
old_400_retry = """                                    shrink_retried = True
                                    log.warning("Stream 400 retry (context/max_tokens) - re-shrinking: %s", _et[:200])
                                    try:
                                        current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))"""
new_400_retry = """                                    shrink_retried = True
                                    log.warning("Stream 400 retry (context/max_tokens) - re-shrinking: %s", _et[:200])
                                    calibrator.bump_margin(session_key)
                                    try:
                                        current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))"""
assert old_400_retry in src, "stream retry 400 block not found"
src = src.replace(old_400_retry, new_400_retry, 1)
print("7. Added bump_margin to stream retry 400")

# ============================================================
# 8. K3: Fix _fetch_session_summary slice
#    Old: s[:budget * 8]  (assumes 8 chars/token)
#    New: s[:int(budget * RHO_COLD)]  (uses calibrated density)
# ============================================================
old_k3 = """            if t <= budget:
                return s
            return s[:budget * 8]"""
new_k3 = """            if t <= budget:
                return s
            return s[:int(budget * RHO_COLD)]"""
assert old_k3 in src, "K3 slice not found"
src = src.replace(old_k3, new_k3, 1)
print("8. Fixed K3: _fetch_session_summary slice uses RHO_COLD")

# ============================================================
# 9. Metrics: Add estimator metrics to prometheus endpoint
# ============================================================
old_metrics_end = """        "# TYPE ctxgate_worker_pending gauge", "ctxgate_worker_pending %d" % _worker_pending,
    ]"""
new_metrics_end = """        "# TYPE ctxgate_worker_pending gauge", "ctxgate_worker_pending %d" % _worker_pending,
        "# TYPE ctxgate_estimator_under_total counter", "ctxgate_estimator_under_total %d" % calibrator.under_total,
        "# TYPE ctxgate_estimator_over_total counter", "ctxgate_estimator_over_total %d" % calibrator.over_total,
        "# TYPE ctxgate_estimator_max_under_pct gauge", "ctxgate_estimator_max_under_pct %.4f" % calibrator.max_under_pct,
        "# TYPE ctxgate_estimator_max_over_pct gauge", "ctxgate_estimator_max_over_pct %.4f" % calibrator.max_over_pct,
        "# TYPE ctxgate_estimator_ratio gauge", "ctxgate_estimator_ratio %.4f" % calibrator.ratio,
    ]"""
assert old_metrics_end in src, "metrics endpoint end not found"
src = src.replace(old_metrics_end, new_metrics_end, 1)
print("9. Added estimator metrics to prometheus")

# ============================================================
# Write result
# ============================================================
with open(PATH, "w") as f:
    f.write(src)

new_lines = src.count("\n") + 1
print(f"New: {new_lines} lines")

# Verify syntax
try:
    ast.parse(src)
    print("Syntax: OK")
except SyntaxError as e:
    print(f"Syntax ERROR: line {e.lineno}: {e.msg}")
    all_lines = src.split("\n")
    for i in range(max(0, e.lineno-3), min(len(all_lines), e.lineno+2)):
        print(f"  {i+1}: {all_lines[i]}")
    sys.exit(1)
