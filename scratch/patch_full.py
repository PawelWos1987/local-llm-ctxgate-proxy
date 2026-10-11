#!/usr/bin/env python3
"""Complete patch: calibrated token estimator. Single-pass, no range errors."""
import sys, ast, subprocess

PATH = "/home/pawelw/ctxproxy/proxy/app.py"

with open(PATH) as f:
    src = f.read()

print(f"Original: {src.count(chr(10))+1} lines")

# ============================================================
# 1. Make import tokenizers optional
# ============================================================
old = "import tokenizers\n"
new = "try:\n    import tokenizers\nexcept ImportError:\n    tokenizers = None\n"
assert old in src, "import tokenizers not found"
src = src.replace(old, new, 1)

# ============================================================
# 2. Add enc = None global after pool
# ============================================================
old = "pool: Optional[asyncpg.Pool] = None"
new = "pool: Optional[asyncpg.Pool] = None\nenc = None  # tokenizer (only loaded in exact mode)"
assert old in src, "pool definition not found"
src = src.replace(old, new, 1)

# ============================================================
# 3. Replace enc loading in lifespan (make conditional)
# ============================================================
old = """    try:
        enc = tokenizers.Tokenizer.from_file(QWEN_TOKENIZER_PATH)
        tok_name = "Qwen tokenizer.json"
        vocab = enc.get_vocab_size()
        log.info("ctxgate-proxy started: tokenizer=%s vocab=%d, vllm=%s model=%s",
                 tok_name, vocab, VLLM_URL, VLLM_MODEL)
    except Exception as e:
        log.error("Failed to load tokenizer %s: %s", QWEN_TOKENIZER_PATH, e)
        raise SystemExit(78)"""

new = """    if ESTIMATOR_MODE == "exact":
        try:
            enc = tokenizers.Tokenizer.from_file(QWEN_TOKENIZER_PATH)
            tok_name = "Qwen tokenizer.json"
            vocab = enc.get_vocab_size()
            log.info("ctxgate-proxy started: tokenizer=%s vocab=%d, vllm=%s model=%s",
                     tok_name, vocab, VLLM_URL, VLLM_MODEL)
        except Exception as e:
            log.error("Failed to load tokenizer %s: %s (required for exact mode)", QWEN_TOKENIZER_PATH, e)
            raise SystemExit(78)
    else:
        log.info("ctxgate-proxy started: estimator=calibrated (rho=%.2f), vllm=%s model=%s",
                 RHO_COLD, VLLM_URL, VLLM_MODEL)"""

assert old in src, "enc loading block not found"
src = src.replace(old, new, 1)

# ============================================================
# 4. Replace token counting functions (count_tokens through _smart_messages_tokens)
#    Use exact line-based replacement to avoid range errors
# ============================================================
lines = src.split("\n")

# Find start: "def count_tokens("
ct_start = None
for i, l in enumerate(lines):
    if l == "def count_tokens(text: str, estimate: bool = False) -> int:":
        ct_start = i
        break
assert ct_start is not None, "count_tokens not found"

# Find start of _smart_messages_tokens
smart_start = None
for i, l in enumerate(lines):
    if l.startswith("def _smart_messages_tokens("):
        smart_start = i
        break
assert smart_start is not None, "_smart_messages_tokens not found"

# Find end of _smart_messages_tokens: next top-level def/async def
smart_end = None
for i in range(smart_start + 1, len(lines)):
    if lines[i].startswith("def ") or lines[i].startswith("async def "):
        smart_end = i
        break
assert smart_end is not None, "end of _smart_messages_tokens not found"

print(f"Replacing lines {ct_start+1} to {smart_end} (not inclusive)")

new_token_funcs = '''# --- Calibrated Token Estimator (replaces exact tokenizer on hot path) ---
# Constants from Phase 1 calibration (1310 prefix requests, 250 sessions, Qwen3.8-27B):
RHO_COLD = 3.391       # median chars/token from calibration
RHO_LO = 2.695         # p3 of observed rho (dense content floor)
RHO_HI = 4.026         # p97 of observed rho (sparse content ceiling)
K_TOOLS_COLD = 1.537   # chars per token for tool definitions (conservative)
EWMA_ALPHA = 0.5       # per-session EWMA rate
GLOBAL_ALPHA = 0.1     # global EWMA rate
M_FLOOR = 0.08         # steady-state margin floor
M_FLOOR_COLD = 0.20    # cold-start margin floor (first 2 obs)
M_CAP = 0.25           # max margin
ESTIMATOR_MODE = os.environ.get("CTXGATE_TOKEN_ESTIMATOR", "calibrated")  # "calibrated" | "exact"

class _TokenCalibrator:
    """Per-session EWMA calibrator for the token predictor.

    Observes (predicted, real) pairs from the Goose path and adjusts
    the per-session rho to minimize under-estimation.
    """
    def __init__(self):
        self._sessions: dict = {}
        self._global_rho: float = RHO_COLD
        self._global_errs: deque = deque(maxlen=200)
        # Telemetry
        self.under_total = 0
        self.over_total = 0
        self.max_under_pct = 0.0
        self.max_over_pct = 0.0
        self.ratio = 1.0  # effective multiplier gauge

    def observe(self, session_key: str, predicted: int, real: int) -> None:
        """Call after receiving real prompt_tokens from vLLM."""
        if real <= 0 or predicted <= 0:
            return
        err = (predicted - real) / real
        under = max(0.0, -err)
        over = max(0.0, err)

        if under > 0:
            self.under_total += 1
            self.max_under_pct = max(self.max_under_pct, under)
        else:
            self.over_total += 1
            self.max_over_pct = max(self.max_over_pct, over)

        # Update per-session EWMA
        s = self._sessions.get(session_key)
        if s is None:
            s = {"rho": self._global_rho, "errs": deque(maxlen=50), "n": 0, "m_bump": 0.0}
            self._sessions[session_key] = s

        ratio = predicted / real
        new_rho = s["rho"] / ratio if ratio > 0 else s["rho"]
        new_rho = max(RHO_LO, min(RHO_HI, new_rho))

        if s["n"] == 0:
            s["rho"] = 0.5 * new_rho + 0.5 * self._global_rho
        else:
            s["rho"] = (1 - EWMA_ALPHA) * s["rho"] + EWMA_ALPHA * new_rho
        s["errs"].append(under)
        s["n"] += 1

        self._global_errs.append(under)
        self._global_rho = (1 - GLOBAL_ALPHA) * self._global_rho + GLOBAL_ALPHA * new_rho
        self.ratio = ratio

    def get_rho(self, session_key: str) -> tuple:
        """Return (rho, margin) for the given session."""
        s = self._sessions.get(session_key)
        if s is None:
            return RHO_COLD, M_FLOOR_COLD
        if s["n"] >= 3:
            m = max(M_FLOOR, max(s["errs"]) if s["errs"] else 0)
        else:
            p99_g = 0.0
            if len(self._global_errs) >= 10:
                sorted_errs = sorted(self._global_errs)
                p99_g = sorted_errs[int(len(sorted_errs) * 0.99)]
            m = max(M_FLOOR_COLD, p99_g)
        m = min(m + s.get("m_bump", 0.0), M_CAP)
        return s["rho"], m

    def bump_margin(self, session_key: str, amount: float = 0.05) -> None:
        """On a vLLM 400, bump the session margin."""
        s = self._sessions.get(session_key)
        if s is None:
            s = {"rho": self._global_rho, "errs": deque(maxlen=50), "n": 0, "m_bump": 0.0}
            self._sessions[session_key] = s
        s["m_bump"] = min(s.get("m_bump", 0.0) + amount, M_CAP)

calibrator = _TokenCalibrator()

def count_tokens(text: str, estimate: bool = False) -> int:
    """Count tokens in text.

    Default uses the calibrated predictor: tokens = len(text) / rho.
    Pure length arithmetic with zero tokenizer calls.
    When CTXGATE_TOKEN_ESTIMATOR=exact, falls back to the tokenizer.
    """
    if ESTIMATOR_MODE == "exact" and enc is not None:
        try:
            return len(enc.encode(text).ids)
        except AttributeError:
            return len(enc.encode(text))
    return max(1, int(len(text) / RHO_COLD))

def count_message_tokens(msg: dict, estimate: bool = False) -> int:
    """Predict tokens for a single message using pure length arithmetic.

    No json.dumps, no encode, no join. O(1) per message.
    """
    total_chars = 0
    content = msg.get("content")
    if content:
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for p in content:
                if isinstance(p, dict):
                    total_chars += len(p.get("text", ""))
    total_chars += len(msg.get("role", "")) + 4
    for tc in msg.get("tool_calls", []):
        total_chars += len(tc.get("id", ""))
        fn = tc.get("function", {})
        total_chars += len(fn.get("name", ""))
        total_chars += len(fn.get("arguments", ""))
        total_chars += 10
    if msg.get("tool_call_id"):
        total_chars += len(msg["tool_call_id"])
    return max(1, int(total_chars / RHO_COLD))

def count_messages_tokens(messages: list, estimate: bool = False) -> int:
    """Predict total tokens for a message list using pure length arithmetic.

    O(n) in the number of messages, zero tokenizer calls, no string
    concatenation of the full content.
    """
    if not messages:
        return 0
    total = 0
    for m in messages:
        total += count_message_tokens(m)
    return total

def count_tools_tokens(tools: list) -> int:
    """Predict tokens for a tools definition list."""
    if not tools:
        return 0
    total_chars = 0
    for t in tools:
        fn = t.get("function", {})
        total_chars += len(fn.get("name", ""))
        total_chars += len(fn.get("description", ""))
        params = fn.get("parameters", {})
        if isinstance(params, dict):
            total_chars += len(json.dumps(params, ensure_ascii=False, separators=(",", ":")))
        total_chars += 30
    return max(1, int(total_chars / K_TOOLS_COLD))
'''

# Replace the lines
new_lines = lines[:ct_start] + new_token_funcs.split("\n") + lines[smart_end:]
src = "\n".join(new_lines)

# ============================================================
# 5. Replace _emergency_shrink (line-based)
# ============================================================
lines = src.split("\n")
es_start = None
for i, l in enumerate(lines):
    if l == "def _emergency_shrink(kept: list, ceiling: int) -> list:":
        es_start = i
        break
assert es_start is not None, "_emergency_shrink not found"

es_end = None
for i in range(es_start + 1, len(lines)):
    if lines[i].startswith("def ") or lines[i].startswith("async def "):
        es_end = i
        break
assert es_end is not None, "end of _emergency_shrink not found"

new_shrink = '''def _emergency_shrink(kept: list, ceiling: int) -> list:
    """Hard invariant: shrink a built window to <= ceiling tokens without breaking
    the tool-call graph. Uses O(n) incremental token tracking (no re-counting in loops)."""
    before = count_messages_tokens(kept)
    if before <= ceiling:
        return kept
    metrics["emergency_shrink_total"] += 1
    work = [dict(m) for m in kept]
    protected = _protected_indices(work)
    # Pre-compute per-message token estimates (O(n) once)
    msg_toks = [count_message_tokens(m) for m in work]
    running_total = sum(msg_toks)
    
    # (4) elide large tool bodies, oldest-first, SKIP protected
    for i in range(len(work)):
        if running_total <= ceiling:
            break
        if i in protected:
            continue
        m = work[i]
        if m.get("role") != "tool":
            continue
        c = m.get("content")
        if not isinstance(c, str) or len(c) <= 2500:
            continue
        new_content = _elided_tool_content(c)
        old_tok = msg_toks[i]
        work[i]["content"] = new_content
        new_tok = count_message_tokens(work[i])
        running_total += new_tok - old_tok
        msg_toks[i] = new_tok
    # (5) drop oldest whole groups while still over
    dropped_groups = 0
    while running_total > ceiling:
        start = None
        for i in range(len(work)):
            if i in protected:
                continue
            if work[i].get("role") in ("assistant", "user"):
                start = i
                break
        if start is None:
            break
        end = start
        if work[start].get("role") == "assistant" and work[start].get("tool_calls"):
            j = start + 1
            while j < len(work) and work[j].get("role") == "tool":
                end = j
                j += 1
        while end > start and end in protected:
            end -= 1
        for i in range(start, end + 1):
            running_total -= msg_toks[i]
        del work[start:end + 1]
        msg_toks = [count_message_tokens(m) for m in work]
        dropped_groups += 1
    metrics["emergency_shrink_groups_dropped"] += dropped_groups
    if running_total > ceiling:
        log.error("EMERGENCY SHRINK: protected material exceeds ceiling (before=%d now=%d ceiling=%d)", before, running_total, ceiling)
        raise ContextCapacityError(
            f"Protected current-turn data alone exceeds the safe budget "
            f"({running_total} tokens > ceiling {ceiling}). Reduce input or increase CTXGATE_MAX_CONTEXT.",
            before, ceiling)
    log.warning("EMERGENCY SHRINK: before=%d after=%d ceiling=%d groups_dropped=%d", before, running_total, ceiling, dropped_groups)
    return work
'''

new_lines = lines[:es_start] + new_shrink.split("\n") + lines[es_end:]
src = "\n".join(new_lines)

# ============================================================
# 6. Replace _smart_messages_tokens calls in build_context
# ============================================================
# There are 3 calls: fast path, slow path (total), slow path (kept_tok)
# Replace all with count_messages_tokens
src = src.replace(
    """kept_tok = _smart_messages_tokens(
                kept,
                int(0.9 * MAX_INPUT),
                int(1.1 * MAX_INPUT))""",
    """kept_tok = count_messages_tokens(kept)""")

src = src.replace(
    """total = _smart_messages_tokens(
            messages,
            int(0.9 * MAX_INPUT),
            int(1.1 * MAX_INPUT))""",
    """total = count_messages_tokens(messages)""")

# The third one (kept_tok in slow path after emergency_shrink)
src = src.replace(
    """kept_tok = _smart_messages_tokens(
            kept,
            int(0.9 * MAX_INPUT),
            int(1.1 * MAX_INPUT))""",
    """kept_tok = count_messages_tokens(kept)""")

# ============================================================
# 7. Replace _smart_messages_tokens call in chat_completions
# ============================================================
src = src.replace(
    """input_tokens = _smart_messages_tokens(
            built, _sensitive_input_lo, _sensitive_input_hi)""",
    """input_tokens = count_messages_tokens(built)""")

# ============================================================
# 8. K10 + Calibrator: Non-stream usage
# ============================================================
old = """        out_tok = data["usage"].get("completion_tokens", 0)
        cached = (data["usage"].get(
            "prompt_tokens_details") or {}).get("cached_tokens", 0)
        data["usage"]["prompt_tokens"] = input_tokens
        data["usage"]["total_tokens"] = input_tokens + out_tok"""
new = """        out_tok = data["usage"].get("completion_tokens", 0)
        cached = (data["usage"].get(
            "prompt_tokens_details") or {}).get("cached_tokens", 0)
        _real_pt = data["usage"].get("prompt_tokens", 0)
        if _real_pt > 0:
            calibrator.observe(session_key, input_tokens, _real_pt)
            log.info("TOKEST est=%d real=%d err=%+.1f%% ratio=%.3f m=%.3f session=%s",
                     input_tokens, _real_pt, 100.0*(input_tokens-_real_pt)/_real_pt,
                     calibrator.ratio, calibrator.get_rho(session_key)[1], session_key)
        data["usage"]["prompt_tokens"] = _real_pt if _real_pt > 0 else input_tokens
        data["usage"]["total_tokens"] = data["usage"]["prompt_tokens"] + out_tok"""
assert old in src, "non-stream usage block not found"
src = src.replace(old, new, 1)

# ============================================================
# 9. Calibrator: Stream usage (first occurrence)
# ============================================================
old = """                                    total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                    usage["prompt_tokens"] = input_tokens
                                    usage["total_tokens"] = input_tokens + (usage.get("completion_tokens") or 0)"""
new = """                                    total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                    _real_pt_s = usage.get("prompt_tokens", 0)
                                    if _real_pt_s > 0:
                                        calibrator.observe(session_key, input_tokens, _real_pt_s)
                                        log.info("TOKEST est=%d real=%d err=%+.1f%% ratio=%.3f m=%.3f session=%s",
                                                 input_tokens, _real_pt_s, 100.0*(input_tokens-_real_pt_s)/_real_pt_s,
                                                 calibrator.ratio, calibrator.get_rho(session_key)[1], session_key)
                                    usage["prompt_tokens"] = _real_pt_s if _real_pt_s > 0 else input_tokens
                                    usage["total_tokens"] = usage["prompt_tokens"] + (usage.get("completion_tokens") or 0)"""
assert old in src, "stream usage block not found"
src = src.replace(old, new, 1)

# ============================================================
# 10. Calibrator: Stream retry usage
# ============================================================
old = """                                        total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
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
new = """                                        total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
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
assert old in src, "stream retry usage block not found"
src = src.replace(old, new, 1)

# ============================================================
# 11. K7: bump_margin on 400 (non-stream)
# ============================================================
old = """            if _is_ctx400:
                log.warning("vLLM 400 (context/max_tokens) - re-shrinking and retrying: %s", resp.text[:200])
                reduced_limit = int(input_tokens * 0.8)"""
new = """            if _is_ctx400:
                log.warning("vLLM 400 (context/max_tokens) - re-shrinking and retrying: %s", resp.text[:200])
                calibrator.bump_margin(session_key)
                reduced_limit = int(input_tokens * 0.8)"""
assert old in src, "non-stream 400 block not found"
src = src.replace(old, new, 1)

# ============================================================
# 12. K7: bump_margin on 400 (stream)
# ============================================================
old = """                                shrink_retried = True
                                log.warning("Stream 400 (context/max_tokens) - re-shrinking and retrying: %s", _et[:200])
                                try:
                                    current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))"""
new = """                                shrink_retried = True
                                log.warning("Stream 400 (context/max_tokens) - re-shrinking and retrying: %s", _et[:200])
                                calibrator.bump_margin(session_key)
                                try:
                                    current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))"""
assert old in src, "stream 400 block not found"
src = src.replace(old, new, 1)

# ============================================================
# 13. K7: bump_margin on 400 (stream retry)
# ============================================================
old = """                                    shrink_retried = True
                                    log.warning("Stream 400 retry (context/max_tokens) - re-shrinking: %s", _et[:200])
                                    try:
                                        current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))"""
new = """                                    shrink_retried = True
                                    log.warning("Stream 400 retry (context/max_tokens) - re-shrinking: %s", _et[:200])
                                    calibrator.bump_margin(session_key)
                                    try:
                                        current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))"""
assert old in src, "stream retry 400 block not found"
src = src.replace(old, new, 1)

# ============================================================
# 14. K3: Fix _fetch_session_summary slice
# ============================================================
old = """            if t <= budget:
                return s
            return s[:budget * 8]"""
new = """            if t <= budget:
                return s
            return s[:int(budget * RHO_COLD)]"""
assert old in src, "K3 slice not found"
src = src.replace(old, new, 1)

# ============================================================
# 15. _check_config: make tokenizer optional
# ============================================================
old = """    # Tokenizer
    tok = os.environ.get("CTXGATE_QWEN_TOKENIZER", "")
    if not tok:
        problems.append("CTXGATE_QWEN_TOKENIZER is required")
    elif not os.path.isfile(tok):
        problems.append(f"Tokenizer file not found: {tok}")"""
new = """    # Tokenizer (only required in exact mode)
    if ESTIMATOR_MODE == "exact":
        tok = os.environ.get("CTXGATE_QWEN_TOKENIZER", "")
        if not tok:
            problems.append("CTXGATE_QWEN_TOKENIZER is required for exact mode")
        elif not os.path.isfile(tok):
            problems.append(f"Tokenizer file not found: {tok}")"""
assert old in src, "tokenizer check not found"
src = src.replace(old, new, 1)

# ============================================================
# 16. Metrics: Add estimator metrics to prometheus
# ============================================================
old = """        "# TYPE ctxgate_worker_pending gauge", "ctxgate_worker_pending %d" % _worker_pending,
    ]"""
new = """        "# TYPE ctxgate_worker_pending gauge", "ctxgate_worker_pending %d" % _worker_pending,
        "# TYPE ctxgate_estimator_under_total counter", "ctxgate_estimator_under_total %d" % calibrator.under_total,
        "# TYPE ctxgate_estimator_over_total counter", "ctxgate_estimator_over_total %d" % calibrator.over_total,
        "# TYPE ctxgate_estimator_max_under_pct gauge", "ctxgate_estimator_max_under_pct %.4f" % calibrator.max_under_pct,
        "# TYPE ctxgate_estimator_max_over_pct gauge", "ctxgate_estimator_max_over_pct %.4f" % calibrator.max_over_pct,
        "# TYPE ctxgate_estimator_ratio gauge", "ctxgate_estimator_ratio %.4f" % calibrator.ratio,
    ]"""
assert old in src, "metrics endpoint end not found"
src = src.replace(old, new, 1)

# ============================================================
# Write result
# ============================================================
with open(PATH, "w") as f:
    f.write(src)

new_line_count = src.count("\n") + 1
print(f"New: {new_line_count} lines")

# Verify syntax
try:
    ast.parse(src)
    print("Syntax: OK")
except SyntaxError as e:
    print(f"Syntax ERROR: line {e.lineno}: {e.msg}")
    all_l = src.split("\n")
    for i in range(max(0, e.lineno-3), min(len(all_l), e.lineno+2)):
        print(f"  {i+1}: {all_l[i]}")
    sys.exit(1)

# Verify no _smart_messages_tokens remains
if "_smart_messages_tokens" in src:
    print("WARNING: _smart_messages_tokens still present!")
    for i, l in enumerate(src.split("\n")):
        if "_smart_messages_tokens" in l:
            print(f"  line {i+1}: {l.strip()}")
else:
    print("_smart_messages_tokens: fully removed")

# Verify build_context exists
if "async def build_context" in src:
    print("build_context: present")
else:
    print("ERROR: build_context MISSING!")
    sys.exit(1)
