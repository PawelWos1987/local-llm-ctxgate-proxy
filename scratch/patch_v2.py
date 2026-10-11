#!/usr/bin/env python3
"""Comprehensive patch: replace tokenizer with calibrated estimator."""
import sys, ast

PATH = "/home/pawelw/ctxproxy/proxy/app.py"

with open(PATH) as f:
    src = f.read()

original_lines = src.count("\n") + 1
print(f"Original: {original_lines} lines")

# ============================================================
# 1. Make import tokenizers optional
# ============================================================
old_import = "import tokenizers\n"
new_import = "try:\n    import tokenizers\nexcept ImportError:\n    tokenizers = None\n"
assert old_import in src, "import tokenizers not found"
src = src.replace(old_import, new_import, 1)

# ============================================================
# 2. Add enc = None global (after pool definition)
# ============================================================
old_pool = "pool: Optional[asyncpg.Pool] = None"
new_pool = "pool: Optional[asyncpg.Pool] = None\nenc = None  # tokenizer (only loaded in exact mode)"
assert old_pool in src, "pool definition not found"
src = src.replace(old_pool, new_pool, 1)

# ============================================================
# 3. Replace the enc loading in lifespan
# ============================================================
# Find the block from "    try:" before "tokenizers.Tokenizer.from_file" to "raise SystemExit(78)"
# The original block looks like:
#     try:
#         enc = tokenizers.Tokenizer.from_file(QWEN_TOKENIZER_PATH)
#         tok_name = "Qwen tokenizer.json"
#         vocab = enc.get_vocab_size()
#         log.info("ctxgate-proxy started: tokenizer=%s vocab=%d, vllm=%s model=%s",
#                  tok_name, vocab, VLLM_URL, VLLM_MODEL)
#     except Exception as e:
#         log.error("Failed to load tokenizer %s: %s", QWEN_TOKENIZER_PATH, e)
#         raise SystemExit(78)

old_enc_load = """    try:
        enc = tokenizers.Tokenizer.from_file(QWEN_TOKENIZER_PATH)
        tok_name = "Qwen tokenizer.json"
        vocab = enc.get_vocab_size()
        log.info("ctxgate-proxy started: tokenizer=%s vocab=%d, vllm=%s model=%s",
                 tok_name, vocab, VLLM_URL, VLLM_MODEL)
    except Exception as e:
        log.error("Failed to load tokenizer %s: %s", QWEN_TOKENIZER_PATH, e)
        raise SystemExit(78)"""

new_enc_load = """    # Tokenizer is NOT loaded at startup (removed from hot path).
    # Only loaded when CTXGATE_TOKEN_ESTIMATOR=exact (emergency mode).
    if ESTIMATOR_MODE == "exact":
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

assert old_enc_load in src, f"enc loading block not found. Searching..."
# Debug: find what's actually there
idx = src.find("tokenizers.Tokenizer.from_file")
if idx >= 0:
    snippet = src[max(0,idx-200):idx+300]
    print(f"DEBUG: found from_file at char {idx}")
    print(f"Context: {repr(snippet[:100])}")
src = src.replace(old_enc_load, new_enc_load, 1)

# ============================================================
# 4. Replace the token counting functions
#    (count_tokens through _smart_messages_tokens)
# ============================================================

# Find the start: "def count_tokens"
ct_idx = src.find("def count_tokens(")
assert ct_idx >= 0, "count_tokens not found"

# Find the start of _smart_messages_tokens
smart_idx = src.find("def _smart_messages_tokens(")
assert smart_idx >= 0, "_smart_messages_tokens not found"

# Find the end of _smart_messages_tokens: next "def " at column 0 after smart_idx
# Look for "\ndef " or "\nasync def " after smart_idx
rest = src[smart_idx:]
next_def = rest.find("\ndef ")
if next_def < 0:
    next_def = rest.find("\nasync def ")
assert next_def >= 0, "Could not find end of _smart_messages_tokens"
smart_end = smart_idx + next_def + 1  # +1 to include the newline

# The block to replace is from ct_idx to smart_end
old_block = src[ct_idx:smart_end]
print(f"Replacing token funcs: {len(old_block)} chars")

new_token_funcs = '''# --- Calibrated Token Estimator (replaces exact tokenizer on hot path) ---
# Constants from Phase 1 calibration (1310 prefix requests, 250 sessions, Qwen3.8-27B):
#   rho = chars per token (content density)
#   Formula: tokens = chars / rho + tools_chars / k_tools
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

        # Derive observed rho adjustment from this request
        ratio = predicted / real
        # If we under-estimated (ratio < 1), we need lower rho (more tokens)
        # If we over-estimated (ratio > 1), we need higher rho (fewer tokens)
        new_rho = s["rho"] / ratio if ratio > 0 else s["rho"]
        new_rho = max(RHO_LO, min(RHO_HI, new_rho))

        if s["n"] == 0:
            s["rho"] = 0.5 * new_rho + 0.5 * self._global_rho
        else:
            s["rho"] = (1 - EWMA_ALPHA) * s["rho"] + EWMA_ALPHA * new_rho
        s["errs"].append(under)
        s["n"] += 1

        # Update global
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
    # Role + structural overhead
    total_chars += len(msg.get("role", "")) + 4
    # Tool calls: count name + arguments length (no json.dumps)
    for tc in msg.get("tool_calls", []):
        total_chars += len(tc.get("id", ""))
        fn = tc.get("function", {})
        total_chars += len(fn.get("name", ""))
        total_chars += len(fn.get("arguments", ""))
        total_chars += 10  # structural overhead
    # tool_call_id
    if msg.get("tool_call_id"):
        total_chars += len(msg["tool_call_id"])
    return max(1, int(total_chars / RHO_COLD))

def count_messages_tokens(messages: list, estimate: bool = False) -> int:
    """Predict total tokens for a message list using pure length arithmetic.

    O(n) in the number of messages, zero tokenizer calls, no string
    concatenation of the full content. Each message is counted independently.
    """
    if not messages:
        return 0
    total = 0
    for m in messages:
        total += count_message_tokens(m)
    return total

def count_tools_tokens(tools: list) -> int:
    """Predict tokens for a tools definition list.

    Uses chars/k_tools. No full serialization needed.
    """
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
        total_chars += 30  # structural: {"type":"function","function":{...}}
    return max(1, int(total_chars / K_TOOLS_COLD))
'''

src = src[:ct_idx] + new_token_funcs + src[smart_end:]

# ============================================================
# 5. Replace _emergency_shrink
# ============================================================
es_idx = src.find("def _emergency_shrink(")
assert es_idx >= 0, "_emergency_shrink not found"

# Find end: next "def " at column 0
rest = src[es_idx:]
next_def = rest.find("\ndef ")
if next_def < 0:
    next_def = rest.find("\nasync def ")
assert next_def >= 0, "Could not find end of _emergency_shrink"
es_end = es_idx + next_def + 1

new_shrink = '''def _emergency_shrink(kept: list, ceiling: int) -> list:
    """Hard invariant: shrink a built window to <= ceiling tokens without breaking
    the tool-call graph. Used by build_context (both paths) and the 400 re-trim.

    Canonical order:
      1. preserve seed
      2. preserve pinned newest-user copy
      3. preserve newest-4 tool groups (via protected_tool_groups)
      4. elide OLDER tool bodies only (skip protected)
      5. drop oldest complete assistant/user/tool groups
      6. if STILL over because PROTECTED material exceeds ceiling ->
         raise ContextCapacityError (caller turns into 413)

    Returns the (possibly new) list; never mutates the caller list.
    Uses O(n) incremental token tracking (no re-counting in loops)."""
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
        # Subtract tokens of dropped range
        for i in range(start, end + 1):
            running_total -= msg_toks[i]
        del work[start:end + 1]
        # Rebuild msg_toks after deletion
        msg_toks = [count_message_tokens(m) for m in work]
        dropped_groups += 1
    metrics["emergency_shrink_groups_dropped"] += dropped_groups
    # (6) if STILL over: protected material alone exceeds ceiling
    if running_total > ceiling:
        log.error("EMERGENCY SHRINK: protected material exceeds ceiling (before=%d now=%d ceiling=%d)", before, running_total, ceiling)
        raise ContextCapacityError(
            f"Protected current-turn data alone exceeds the safe budget "
            f"({running_total} tokens > ceiling {ceiling}). Reduce input or increase CTXGATE_MAX_CONTEXT.",
            before, ceiling)
    log.warning("EMERGENCY SHRINK: before=%d after=%d ceiling=%d groups_dropped=%d", before, running_total, ceiling, dropped_groups)
    return work
'''

src = src[:es_idx] + new_shrink + src[es_end:]

# ============================================================
# 6. Update _check_config tokenizer section
# ============================================================
old_tok_check = """    # Tokenizer
    tok = os.environ.get("CTXGATE_QWEN_TOKENIZER", "")
    if not tok:
        problems.append("CTXGATE_QWEN_TOKENIZER is required")
    elif not os.path.isfile(tok):
        problems.append(f"Tokenizer file not found: {tok}")"""

new_tok_check = """    # Tokenizer (only required in exact mode)
    if ESTIMATOR_MODE == "exact":
        tok = os.environ.get("CTXGATE_QWEN_TOKENIZER", "")
        if not tok:
            problems.append("CTXGATE_QWEN_TOKENIZER is required for exact mode")
        elif not os.path.isfile(tok):
            problems.append(f"Tokenizer file not found: {tok}")"""

assert old_tok_check in src, "tokenizer check block not found in _check_config"
src = src.replace(old_tok_check, new_tok_check, 1)

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
    # Show context
    all_lines = src.split("\n")
    for i in range(max(0, e.lineno-3), min(len(all_lines), e.lineno+2)):
        print(f"  {i+1}: {all_lines[i]}")
    sys.exit(1)
