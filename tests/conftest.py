import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# --- Environment for test isolation ---
# Use DIRECT assignment (not setdefault) to ensure test isolation
# regardless of what's in the shell environment.
# These MUST be set before proxy.app is imported (module-level validate_config).
# Also set CTXGATE_SKIP_DOTENV=1 to prevent .env file from interfering.
os.environ["CTXGATE_SKIP_DOTENV"] = "1"
os.environ["CTXGATE_DB_DSN"] = "postgresql://test:test@127.0.0.1:5432/ctxproxy_test"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:9298"
os.environ["CTXGATE_VLLM_MODEL"] = "test-model"
os.environ["CTXGATE_ALLOW_NO_AUTH"] = "1"
os.environ["CTXGATE_HOST"] = "127.0.0.1"
os.environ["CTXGATE_PORT"] = "9299"
os.environ["CTXGATE_QWEN_TOKENIZER"] = ""
os.environ["CTXGATE_LM_ENABLED"] = "0"
os.environ["CTXGATE_LM_URL"] = "http://127.0.0.1:9298/v1/chat/completions"
os.environ["CTXGATE_LM_API_KEY"] = "test-key"
os.environ["CTXGATE_GOOSE_INTEGRATION"] = "0"
os.environ["CTXGATE_SESSION_IDENTITY"] = "goose"
os.environ["CTXGATE_API_KEY"] = ""
os.environ["CTXGATE_API_KEYS"] = ""
os.environ["CTXGATE_ADMIN_KEY"] = ""
os.environ["CTXGATE_MAX_CONTEXT"] = "84000"
os.environ["CTXGATE_MAX_INPUT"] = "58000"
os.environ["CTXGATE_MAX_OUTPUT"] = "22500"
os.environ["CTXGATE_SAFETY_MARGIN"] = "3500"
os.environ["CTXGATE_MIN_OUTPUT"] = "128"
os.environ["CTXGATE_MIN_CONTINUATION_OUTPUT"] = "64"
os.environ["CTXGATE_MAX_TOTAL_OUTPUT"] = "8192"
os.environ["CTXGATE_MAX_REASONING_TOKENS"] = "1000"
os.environ["CTXGATE_LOOP_RETRIES"] = "1"
os.environ["CTXGATE_BLOCK_UNBOUNDED_LOOPS"] = "1"
os.environ["CTXGATE_WORKER_POLL"] = "2.0"
os.environ["CTXGATE_WORKER_MAX_ATTEMPTS"] = "3"
os.environ["CTXGATE_WORKER_OUTAGE_TTL"] = "1800"
os.environ["CTXGATE_WORKER_MAX_TOKENS"] = "2048"
os.environ["CTXGATE_MEMORY_TTL_DAYS"] = "90"
os.environ["CTXGATE_PROXY_PORT"] = "9299"

# Tests excluded from pytest collection:
# - Script-style: execute code at module level, use print-based assertions, call sys.exit()
# - Integration: require live proxy (port 9201), PostgreSQL, or vLLM server
# Run them standalone: python3 tests/<file>.py
collect_ignore = [
    # Script-style (module-level execution, not pytest-compatible)
    "test_knowledge_sharing.py",   # sys.exit() at module level
    "test_phase1.py",             # script-style, requires ctxproxy_test DB
    "test_loop_guard.py",         # module-level record() calls
    "test_architecture.py",       # requires live proxy + worker
    "test_tool_gauntlet.py",      # live HTTP calls to proxy:9201
    "test_unit_integ.py",         # live HTTP calls to proxy
    "test_e2e_4b_memory.py",      # script-style e2e test
    "test_f13_dedup.py",          # script-style
    "test_f14_false_positives.py",# script-style
    "test_memory_prefix.py",      # script-style, requires PG
    "test_phase1b.py",            # script-style
    "test_phase2.py",             # script-style
    "test_s1.py",                 # script-style
    "test_scenario_replay.py",    # script-style
    "test_deliverable.py",        # script-style, requires live proxy on port 9209
    # Integration (require live proxy on port 9201)
    "test_session_isolation.py",  # urllib.request to proxy:9201
    "test_dashboard.py",          # urllib.request to live proxy
    "test_sigterm_graceful_shutdown.py",  # requires live proxy
    "test_streaming_at_cap.py",   # requires live proxy
]
