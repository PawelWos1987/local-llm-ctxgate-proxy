"""J3: SUPERSEDE must be transactional (INSERT + UPDATE in one transaction).

Without a transaction, if the INSERT succeeds but the UPDATE fails (or the
process dies between them), the new memory row exists but the old one is never
marked superseded — leaving two active memories with the same key.

Fix: wrap the SUPERSEDE branch in an asyncpg transaction on a single
acquired connection so INSERT + UPDATE are atomic.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["CTXGATE_DB_DSN"] = "postgresql://localhost:5432/ctxproxy"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:29000/v1"
os.environ["CTXGATE_QWEN_TOKENIZER"] = " "

import proxy.app as app  # noqa: E402





