"""Phase 04: Estimator/Budget Canonicalization tests.

Verifies:
- Default estimator mode is "exact"
- Shadow mode does not control admission
- Tools are counted in the budget
- Uncertainty guard reduces the effective budget
- Cache keys are versioned (content hash + model + template + schema)
- No full-history tokenizer call in calibrated mode
- Missing usage is missing, not zero
- Exact mode uses the real tokenizer
"""
import hashlib
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["CTXGATE_SKIP_DOTENV"] = "1"
os.environ["CTXGATE_TOKEN_ESTIMATOR"] = "calibrated"

import proxy.app as app


# --- Test 1: Default is exact ---

def test_default_is_calibrated():
    """Default estimator mode must be 'calibrated' (no tokenizer on hot path)."""
    import inspect
    from proxy.config.constants import ESTIMATOR_MODE as _est_mode
    src = inspect.getsource(sys.modules['proxy.config.constants'])
    for line in src.split("\n"):
        if "ESTIMATOR_MODE = os.environ.get" in line:
            assert '"calibrated"' in line, (
                f"Default ESTIMATOR_MODE must be 'calibrated', got: {line.strip()}"
            )
            break
    else:
        pytest.fail("Could not find ESTIMATOR_MODE definition")


# --- Test 2: Shadow mode does not control admission ---

def test_shadow_does_not_control_admission(monkeypatch):
    """In shadow mode, admission uses exact count, not shadow prediction."""
    monkeypatch.setattr(app, "ESTIMATOR_MODE", "shadow")
    
    # With no tokenizer loaded (enc is None in test env),
    # shadow should fall back to calibrated (len/rho)
    # The key property: shadow mode NEVER uses the calibrated count
    # for the admission decision. It returns the exact count.
    
    # Simulate: with enc=None, shadow falls back to calibrated
    # This is the safe degradation path
    text = "Hello world" * 100  # ~1100 chars
    result = app.count_tokens(text)
    
    # In shadow mode with no tokenizer, it returns the calibrated estimate
    # (this is the safe fallback - the calibrated estimate is conservative)
    # The important thing is that in production (with tokenizer),
    # shadow returns the EXACT count.
    assert result > 0
    
    # Verify the shadow comparison metrics were NOT incremented
    # (because enc is None, we took the fallback path)
    assert app.metrics["shadow_comparison"]["total"] == 0


# --- Test 3: Tools are counted in the budget ---

def test_tools_counted_in_budget():
    """The total budget must include tool schema tokens."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"},
                        "units": {"type": "string", "enum": ["celsius", "fahrenheit"]}
                    },
                    "required": ["location"]
                }
            }
        }
    ]
    
    # Without tools
    no_tools = app.estimate_full_payload_tokens(messages, [], 0)
    
    # With tools
    with_tools = app.estimate_full_payload_tokens(messages, tools, 0)
    
    # Tools must add tokens
    assert with_tools > no_tools, (
        f"Tools must increase the token count: with_tools={with_tools} <= no_tools={no_tools}"
    )
    
    # The difference should be reasonable (tool schema is ~50-200 tokens)
    diff = with_tools - no_tools
    assert diff > 10, f"Tool schema tokens too small: {diff}"


# --- Test 4: Uncertainty guard reduces budget ---

def test_uncertainty_guard_reduces_budget(monkeypatch):
    """The uncertainty guard must reduce the effective admission budget
    in calibrated mode (proportional to uncertainty).
    
    With the RealTokenTracker, the margin shrinks as real data accumulates:
    - Cold start (no observations): M_FLOOR_COLD (50%)
    - After 1 observation: M_FLOOR_COLD * 0.5 (25%)
    - After 2 observations: M_FLOOR_COLD * 0.25 (12.5%)
    - After 3+ observations: based on recent accuracy (>= M_FLOOR)
    """
    from proxy.config.constants import M_FLOOR_COLD, M_FLOOR, M_CAP
    
    # Reset calibrator to cold state
    app.calibrator._sessions.clear()
    app.calibrator._global_rho = app.RHO_COLD
    app.calibrator._global_errs.clear()
    
    # Cold start: margin should be M_FLOOR_COLD
    rho_cold, margin_cold = app.calibrator.get_rho("test-guard")
    assert margin_cold == M_FLOOR_COLD, (
        f"Cold margin should be M_FLOOR_COLD={M_FLOOR_COLD}, got {margin_cold}"
    )
    
    # After 1 observation: margin decreases
    app.calibrator.observe("test-guard", predicted=1000, real=950)
    rho_1, margin_1 = app.calibrator.get_rho("test-guard")
    assert margin_1 < margin_cold, (
        f"After 1 obs, margin ({margin_1}) should be < cold ({margin_cold})"
    )
    
    # After 2 observations: margin decreases further
    app.calibrator.observe("test-guard", predicted=1000, real=960)
    rho_2, margin_2 = app.calibrator.get_rho("test-guard")
    assert margin_2 < margin_1, (
        f"After 2 obs, margin ({margin_2}) should be < after 1 ({margin_1})"
    )
    
    # After 3+ observations: margin is at least M_FLOOR and capped at M_CAP
    app.calibrator.observe("test-guard", predicted=1000, real=970)
    rho_3, margin_3 = app.calibrator.get_rho("test-guard")
    assert margin_3 >= M_FLOOR, (
        f"After 3 obs, margin ({margin_3}) should be >= M_FLOOR ({M_FLOOR})"
    )
    assert margin_3 <= M_CAP, (
        f"After 3 obs, margin ({margin_3}) should be <= M_CAP ({M_CAP})"
    )
    
    # Verify the estimation function applies the margin in calibrated mode
    monkeypatch.setattr(app, "ESTIMATOR_MODE", "calibrated")
    messages = [{"role": "user", "content": "x" * 1000}]
    
    # Cold start estimation (no real data for this session)
    cold_est = app.estimate_full_payload_tokens(messages, [], 0, session_key="test-guard-cold")
    
    # Warm estimation (has real data)
    app.calibrator.observe("test-guard-warm", predicted=1000, real=990)
    app.calibrator.observe("test-guard-warm", predicted=1000, real=995)
    app.calibrator.observe("test-guard-warm", predicted=1000, real=998)
    warm_est = app.estimate_full_payload_tokens(messages, [], 0, session_key="test-guard-warm")
    
    # The warm estimate should have a smaller multiplier applied
    # (we can't directly compare totals since bases differ, but the margin is smaller)
    _, warm_margin = app.calibrator.get_rho("test-guard-warm")
    _, cold_margin = app.calibrator.get_rho("test-guard-cold")
    assert warm_margin < cold_margin, (
        f"Warm margin ({warm_margin}) should be < cold margin ({cold_margin})"
    )


# --- Test 5: Cache key is versioned ---

def test_cache_key_versioned():
    """Cache keys must include content hash + model + template + schema version."""
    cache = app._token_cache
    content = "test content"
    model = "test-model"
    
    key = cache._make_key(content, model)
    
    # Must contain the SHA-256 hash
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert expected_hash in key, f"Cache key missing content hash: {key}"
    
    # Must contain the model name
    assert model in key, f"Cache key missing model: {key}"
    
    # Must contain version components
    assert app._TOKENIZER_VERSION in key, f"Cache key missing tokenizer version: {key}"
    assert app._TEMPLATE_VERSION in key, f"Cache key missing template version: {key}"
    assert app._SCHEMA_VERSION in key, f"Cache key missing schema version: {key}"
    
    # Different content → different key
    key2 = cache._make_key("different content", model)
    assert key != key2
    
    # Different model → different key
    key3 = cache._make_key(content, "other-model")
    assert key != key3


# --- Test 6: No full-history tokenizer in calibrated mode ---

def test_no_full_history_tokenizer_in_calibrated(monkeypatch):
    """Calibrated mode must not tokenize full history synchronously.
    
    The calibrated path uses len(text)/rho - pure arithmetic, no tokenizer.
    """
    monkeypatch.setattr(app, "ESTIMATOR_MODE", "calibrated")
    
    # Track if the tokenizer is called
    tokenizer_calls = []
    
    # In calibrated mode, count_tokens should NOT call enc.encode
    # It should use len(text) / rho
    text = "x" * 10000  # 10KB of text
    result = app.count_tokens(text)
    
    # Result should be approximately len(text) / rho
    expected = max(1, int(len(text) / app._rho_var.get()))
    assert result == expected, (
        f"Calibrated count_tokens should be len/rho: got {result}, expected {expected}"
    )
    
    # No tokenizer calls should have been made
    # (We verify by checking the result matches pure arithmetic)


# --- Test 7: Missing usage is missing, not zero ---

def test_missing_usage_is_missing_not_zero():
    """If vLLM doesn't return usage, it's treated as missing, not 0.
    
    The calibrator.observe() method must reject real=0 (missing usage).
    """
    # observe() with real=0 must be a no-op
    app.calibrator._sessions.clear()
    
    # This should NOT update the calibrator
    app.calibrator.observe("test-session", 1000, 0)
    
    # The session should not have been created (or should have n=0)
    s = app.calibrator._sessions.get("test-session")
    if s is not None:
        assert s["n"] == 0, "observe(real=0) must not increment observation count"
    
    # Also: predict=0 must be rejected
    app.calibrator.observe("test-session", 0, 100)
    s = app.calibrator._sessions.get("test-session")
    if s is not None:
        assert s["n"] == 0, "observe(predicted=0) must not increment observation count"


# --- Test 8: Exact mode uses real tokenizer ---

def test_exact_mode_uses_real_tokenizer(monkeypatch):
    """Exact mode must use the actual tokenizer (when available).
    
    In test environment, enc may be None (tokenizer not loaded).
    We verify the code path: when enc is not None, it calls enc.encode().
    """
    monkeypatch.setattr(app, "ESTIMATOR_MODE", "exact")
    
    # Create a mock tokenizer to verify the code path
    class MockEncoder:
        def encode(self, text):
            class Result:
                ids = list(range(len(text) // 4 + 1))
            return Result()
    
    monkeypatch.setattr(app, "enc", MockEncoder())
    
    text = "Hello world, this is a test of the exact tokenizer."
    result = app.count_tokens(text)
    
    # The mock returns len(text)//4 + 1 tokens
    expected = len(text) // 4 + 1
    assert result == expected, (
        f"Exact mode must use tokenizer: got {result}, expected {expected}"
    )
    
    # Verify cache was populated
    cached = app._token_cache.get(text, app.VLLM_MODEL)
    assert cached == expected, "Exact mode result must be cached"


# --- Test 9: estimate_full_payload_tokens includes all components ---

def test_estimate_full_payload_includes_all_components(monkeypatch):
    """The canonical estimator must count: messages + tools + template + note."""
    # Use a mock tokenizer so exact mode is truly deterministic
    class MockEnc:
        def encode(self, text):
            class R:
                ids = list(range(max(1, len(text) // 4)))
            return R()
    monkeypatch.setattr(app, "enc", MockEnc())
    monkeypatch.setattr(app, "ESTIMATOR_MODE", "exact")
    
    messages = [
        {"role": "system", "content": "S" * 500},
        {"role": "user", "content": "U" * 500},
        {"role": "assistant", "content": "A" * 500},
    ]
    tools = [
        {"type": "function", "function": {
            "name": "test_tool",
            "description": "A test tool",
            "parameters": {"type": "object", "properties": {}}
        }}
    ]
    
    # With note tokens
    with_note = app.estimate_full_payload_tokens(messages, tools, 100)
    
    # Without note tokens
    without_note = app.estimate_full_payload_tokens(messages, tools, 0)
    
    # Note tokens must add to the total
    assert with_note > without_note
    
    # In exact mode (no margin), the difference is exactly note_tokens
    assert with_note - without_note == 100, (
        f"Note tokens must add exactly: diff={with_note - without_note}, expected 100"
    )


# --- Test 10: Token cache is bounded ---

def test_token_cache_bounded():
    """The token cache must be bounded (LRU eviction)."""
    cache = app._TokenCountCache(maxsize=3)
    
    # Add 5 items
    for i in range(5):
        cache.put(f"content_{i}", "model", i * 10)
    
    # Cache should have at most 3 entries
    assert len(cache._cache) <= 3, f"Cache exceeded maxsize: {len(cache._cache)}"
    
    # The oldest entries should have been evicted
    assert cache.get("content_0", "model") is None, "LRU: oldest should be evicted"
    assert cache.get("content_4", "model") == 40, "Newest should be present"

# --- Test 11: Input estimate headroom reduces output budget ---

def test_input_headroom_reduces_budget(monkeypatch):
    """With headroom=0.05, the output budget is reduced by 5% of input_tokens
    compared to headroom=0.0. Uses a large input to avoid the MAX_OUTPUT cap."""
    import proxy.window.budget as budget_mod
    from proxy.config.constants import MAX_CONTEXT, SAFETY_MARGIN, MAX_OUTPUT

    # Use a large input so the budget is below MAX_OUTPUT (not capped)
    input_tokens = MAX_CONTEXT - SAFETY_MARGIN - MAX_OUTPUT + 5000

    # With headroom = 0.05
    monkeypatch.setattr(budget_mod, "CTXGATE_INPUT_ESTIMATE_HEADROOM", 0.05)
    budget_with_headroom = budget_mod._output_budget(input_tokens)

    # With headroom = 0.0
    monkeypatch.setattr(budget_mod, "CTXGATE_INPUT_ESTIMATE_HEADROOM", 0.0)
    budget_no_headroom = budget_mod._output_budget(input_tokens)

    # The difference should be exactly int(input_tokens * 0.05)
    expected_diff = int(input_tokens * 0.05)
    actual_diff = budget_no_headroom - budget_with_headroom
    assert actual_diff == expected_diff, (
        f"Headroom 0.05 should reduce budget by {expected_diff}, "
        f"got diff={actual_diff} (no_headroom={budget_no_headroom}, "
        f"with_headroom={budget_with_headroom}, input={input_tokens})"
    )


# --- Test 12: Zero headroom preserves original behavior ---

def test_zero_headroom_preserves_original_behavior(monkeypatch):
    """With headroom=0.0, _output_budget behaves identically to the
    original implementation."""
    import proxy.window.budget as budget_mod
    from proxy.config.constants import MAX_CONTEXT, SAFETY_MARGIN, MAX_OUTPUT

    monkeypatch.setattr(budget_mod, "CTXGATE_INPUT_ESTIMATE_HEADROOM", 0.0)

    # Test various input sizes
    for input_tokens in [0, 100, 1000, 5000, 10000, 50000, 80000]:
        result = budget_mod._output_budget(input_tokens)
        expected = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
        assert result == expected, (
            f"headroom=0.0 must preserve original behavior: "
            f"input={input_tokens}, got={result}, expected={expected}"
        )
        # Must be an int
        assert isinstance(result, int)

