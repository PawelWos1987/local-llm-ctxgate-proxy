#!/usr/bin/env python3
"""Phase 1b: Output-path integrity characterization tests.

Runs stream_to_vllm against a mock vLLM server for 8 scenarios.
Captures wire output (raw SSE bytes) as golden files.

Usage:
  python3 tests/test_phase1b.py           # run all scenarios, save goldens
  python3 tests/test_phase1b.py --check   # compare against existing goldens

Gate: (a),(b) byte-identical before/after; (c)-(h) match intended behavior.
"""
import asyncio
import json
import os
import sys
import subprocess
import time
import urllib.request

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "goldens")
MOCK_PORT = 9300
MOCK_URL = "http://127.0.0.1:%d" % MOCK_PORT

SCENARIOS = ["a", "b", "c", "d", "e", "e2", "f", "g", "h"]

def start_mock_server():
    """Start the mock vLLM server as a subprocess."""
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "mock_vllm.py"),
         "--port", str(MOCK_PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Wait for it to be ready
    for _ in range(50):
        try:
            urllib.request.urlopen(MOCK_URL + "/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("Mock vLLM server did not start")

def stop_mock_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

def collect_sse(gen):
    """Collect all SSE strings from an async generator."""
    chunks = []
    async def _drain():
        async for c in gen:
            chunks.append(c)
    asyncio.run(_drain())
    return "".join(chunks)

def run_scenario(scenario):
    """Run one scenario through stream_to_vllm and return the SSE output."""
    # Reset the mock server's call counter via a special request
    import urllib.request
    try:
        req = urllib.request.Request(MOCK_URL + "/reset", data=b"{}", method="POST")
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass
    # Import the app module with mock vLLM URL
    os.environ["CTXGATE_VLLM_URL"] = MOCK_URL
    # We need to reset the module-level state for each scenario
    # The mock server uses the scenario name from the request body
    import importlib
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
    if "app" in sys.modules:
        del sys.modules["app"]
    import app
    
    # Set the VLLM_URL to our mock
    app.VLLM_URL = MOCK_URL
    # Create an httpx client pointing at the mock server
    import httpx
    app._vllm_client = httpx.AsyncClient(timeout=30.0)
    
    # Create a minimal vllm_body
    vllm_body = {
        "model": "mock-model",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, please do something."},
        ],
        "temperature": 0.7,
        "stream": True,
        "stream_options": {"include_usage": True},
        "mock_scenario": scenario,
    }
    
    input_tokens = 100
    session_key = "test:" + scenario
    
    # Run the streaming generator
    # stream_to_vllm is async, returns a StreamingResponse wrapping generate()
    chunks = []
    async def _run():
        resp = await app.stream_to_vllm(vllm_body, input_tokens, session_key)
        gen = resp.body_iterator
        async for c in gen:
            chunks.append(c)
    asyncio.run(_run())
    
    return "".join(chunks)

def main():
    check_mode = "--check" in sys.argv
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    
    proc = start_mock_server()
    passed = 0
    failed = 0
    try:
        for sc in SCENARIOS:
            print("Running scenario %s..." % sc, flush=True)
            output = run_scenario(sc)
            golden_path = os.path.join(GOLDEN_DIR, "scenario_%s.sse" % sc)
            
            if check_mode:
                if not os.path.exists(golden_path):
                    print("  MISSING golden: %s" % golden_path)
                    failed += 1
                    continue
                with open(golden_path, "r") as f:
                    expected = f.read()
                if output == expected:
                    print("  PASS: byte-identical (%d bytes)" % len(output))
                    passed += 1
                else:
                    print("  FAIL: differs from golden")
                    # Show first difference
                    for i, (a, b) in enumerate(zip(output, expected)):
                        if a != b:
                            print("    first diff at byte %d: got %r expected %r" % (i, a, b))
                            print("    context: ...%r... vs ...%r..." % (
                                output[max(0,i-40):i+40], expected[max(0,i-40):i+40]))
                            break
                    if len(output) != len(expected):
                        print("    length: got %d expected %d" % (len(output), len(expected)))
                    failed += 1
            else:
                with open(golden_path, "w") as f:
                    f.write(output)
                print("  Saved golden: %s (%d bytes)" % (golden_path, len(output)))
                passed += 1
    finally:
        stop_mock_server(proc)
    
    print("\n=== Phase 1b: %d passed, %d failed ===" % (passed, failed))
    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
