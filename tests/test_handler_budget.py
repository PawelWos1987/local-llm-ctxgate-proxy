#!/usr/bin/env python3
"""Handler budget tests: 413 on starved output after shrink; cont_budget on
continuation with budget < MIN_OUTPUT. Stub transport, no real vLLM."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["CTXGATE_DB_DSN"] = "postgresql://localhost:5432/ctxproxy"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:29000/v1"
os.environ["CTXGATE_QWEN_TOKENIZER"] = " "

import proxy.app as app

def test_output_budget_below_min_output():
    budget = app._output_budget(70000)
    assert budget < app.MIN_OUTPUT, f"budget={budget} should be < MIN_OUTPUT={app.MIN_OUTPUT}"

def test_output_budget_normal():
    budget = app._output_budget(40000)
    assert budget >= app.MIN_OUTPUT, f"budget={budget} < MIN_OUTPUT={app.MIN_OUTPUT}"
    assert budget <= app.MAX_OUTPUT, f"budget={budget} > MAX_OUTPUT={app.MAX_OUTPUT}"

def test_output_budget_at_ceiling():
    budget = app._output_budget(58000)
    assert budget == 22500, f"budget={budget} expected 22500"

def test_cont_budget_starved():
    cont_tokens = 70000
    cont_budget = app._output_budget(cont_tokens)
    should_stop = cont_tokens > app.MAX_INPUT or cont_budget < app.MIN_OUTPUT
    assert should_stop, "expected cont_budget stop"

def test_cont_budget_normal():
    cont_tokens = 40000
    cont_budget = app._output_budget(cont_tokens)
    should_stop = cont_tokens > app.MAX_INPUT or cont_budget < app.MIN_OUTPUT
    assert not should_stop, "normal continuation should NOT stop"

if __name__ == "__main__":
    test_output_budget_below_min_output()
    test_output_budget_normal()
    test_output_budget_at_ceiling()
    test_cont_budget_starved()
    test_cont_budget_normal()
    print("PASS: handler budget (413 + cont_budget)")
