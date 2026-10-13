#!/usr/bin/env python3
"""Run the Mistral preservation test suite.
Usage: python3 run_tests.py [test_number]
  No arg: runs all 10 tests
  Number: runs just that test (1-10)
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PGPASSWORD", "11!!AdaMicPaw")

from test_mistral_preservation import (
    test_01, test_02, test_03, test_04, test_05,
    test_06, test_07, test_08, test_09, test_10,
    run_all,
)

TESTS = {
    "1": test_01, "2": test_02, "3": test_03, "4": test_04, "5": test_05,
    "6": test_06, "7": test_07, "8": test_08, "9": test_09, "10": test_10,
}

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "all":
        fn = TESTS.get(sys.argv[1])
        if fn is None:
            print("Unknown test number: " + sys.argv[1])
            sys.exit(1)
        result = asyncio.run(fn())
        st = "PASS" if result.passed else "FAIL"
        print("Result: " + st)
        for e in result.errors:
            print("  Error: " + e)
    else:
        asyncio.run(run_all())
