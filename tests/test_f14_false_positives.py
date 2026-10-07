#!/usr/bin/env python3
"""F14: Loop-detector false-positive corpus.

Verifies that _detect_loop does NOT trigger on legitimate repetitive content:
- Long code blocks with similar structure (for-loops, function definitions)
- Markdown tables with repeating column patterns
- Repeated test output lines
- Polish text with repeated words
- JSON with repeated keys

Each sample must return 0 (no loop detected).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
os.environ.setdefault("CTXGATE_DB_DSN", "postgresql://x@127.0.0.1:1/unused")
os.environ.setdefault("VLLM_URL", "http://127.0.0.1:19999/v1")
os.environ.setdefault("VLLM_API_KEY", "x")

import app as appmod

def _check(name, text, expect_loop=False):
    result = appmod._detect_loop(text)
    if expect_loop:
        status = "PASS" if result else "FAIL"
    else:
        status = "PASS" if result == 0 else "FAIL"
    print(f"  {status}: {name} (result={result})")
    return status == "PASS"

def main():
    passed = 0
    failed = 0

    # --- Legitimate content that must NOT trigger ---

    # 1. Python for-loop code (repetitive structure)
    code_block = """
def process_items(items):
    for item in items:
        if item.valid:
            result.append(transform(item))
    for item in results:
        if item.status == "ok":
            output.append(format(item))
    for item in output:
        if item.length < 100:
            final.append(item)
    return final
"""
    if _check("python_for_loops", code_block * 3): passed += 1
    else: failed += 1

    # 2. Markdown table with repeating structure
    md_table = """
| Name | Status | Score |
|------|--------|-------|
| alpha | PASS   | 95    |
| beta  | PASS   | 92    |
| gamma | PASS   | 88    |
| delta | PASS   | 85    |
| eps   | PASS   | 82    |
"""
    if _check("markdown_table", md_table * 4): passed += 1
    else: failed += 1

    # 3. Test output with repeated "PASS" lines
    test_output = """
test_alpha ... PASS
test_beta ... PASS
test_gamma ... PASS
test_delta ... PASS
test_eps ... PASS
test_zeta ... PASS
test_eta ... PASS
test_theta ... PASS
"""
    if _check("test_output_pass", test_output * 5): passed += 1
    else: failed += 1

    # 4. Polish text with repeated words
    polish = """
Zażółć gęślą jaźń. Zażółć gęślą jaźń. Zażółć gęślą jaźń.
To jest test. To jest test. To jest test.
Witaj świecie. Witaj świecie. Witaj świecie.
"""
    if _check("polish_repeated", polish * 3): passed += 1
    else: failed += 1

    # 5. JSON with repeated keys (legitimate array of objects)
    import json
    json_arr = json.dumps([{"name": f"item_{i}", "status": "ok", "value": i} for i in range(20)], indent=2)
    if _check("json_array", json_arr): passed += 1
    else: failed += 1

    # 6. Repeated function signatures (code review)
    sigs = """
def handler_a(request): return response
def handler_b(request): return response
def handler_c(request): return response
def handler_d(request): return response
def handler_e(request): return response
"""
    if _check("function_sigs", sigs * 4): passed += 1
    else: failed += 1

    # 7. SQL with repeated structure
    sql = """
SELECT id, name, status FROM users WHERE status = 'active';
SELECT id, name, status FROM orders WHERE status = 'active';
SELECT id, name, status FROM products WHERE status = 'active';
SELECT id, name, status FROM invoices WHERE status = 'active';
"""
    if _check("sql_repeated", sql * 3): passed += 1
    else: failed += 1

    # 8. YAML config with repeating keys
    yaml = """
service_a:
  port: 8080
  host: localhost
service_b:
  port: 8081
  host: localhost
service_c:
  port: 8082
  host: localhost
"""
    if _check("yaml_repeated", yaml * 3): passed += 1
    else: failed += 1

    # --- Actual loops that MUST trigger ---

    # 9. True reasoning loop (same sentence repeated)
    true_loop = "I need to think about this carefully. I need to think about this carefully. I need to think about this carefully. I need to think about this carefully. I need to think about this carefully. I need to think about this carefully."
    if _check("true_reasoning_loop", true_loop, expect_loop=True): passed += 1
    else: failed += 1

    # 10. True content loop (same word repeated)
    true_content = "hello hello hello hello hello hello hello hello hello hello hello hello hello hello hello hello hello hello hello hello"
    if _check("true_content_loop", true_content, expect_loop=True): passed += 1
    else: failed += 1

    print(f"\n=== F14: {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
