"""Configuration constants for ctxgate-proxy.

All CTXGATE_* environment variables, budget values, prompts, and
importance maps are defined here.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Optional

from proxy.config.env import _env_int, _env_float

# --- VLLM backend ---
VLLM_URL = os.environ.get("CTXGATE_VLLM_URL", "http://127.0.0.1:29000/v1")
VLLM_MODEL = os.environ.get("CTXGATE_VLLM_MODEL", "Qwen3.8-27B")

# ─── Budget Configuration ───────────────────────────────────────────────────
# Primary source: Goose custom provider config (single source of truth).
# Fallback: env vars (CTXGATE_MAX_CONTEXT, CTXGATE_MAX_OUTPUT) → hardcoded defaults.
# All other budget values are derived to maintain the invariant:
#   MAX_INPUT + SAFETY_MARGIN + MAX_OUTPUT <= MAX_CONTEXT

_GOOSE_CONFIG_PATH = os.path.expanduser("~/.config/goose/custom_providers/custom_qwen.json")


def _load_goose_budget() -> tuple:
    """Read context_limit and max_tokens from the Goose custom provider config.
    Falls back to (None, None) if the file is missing or malformed."""
    try:
        with open(_GOOSE_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        for m in cfg.get("models", []):
            ctx = m.get("context_limit")
            out = m.get("max_tokens")
            if ctx and out:
                return int(ctx), int(out)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None, None


_goose_ctx, _goose_out = _load_goose_budget()
MAX_CONTEXT = _env_int("CTXGATE_MAX_CONTEXT", _goose_ctx or 84000)
MAX_OUTPUT = _env_int("CTXGATE_MAX_OUTPUT", _goose_out or int(MAX_CONTEXT * 0.20))

# Derived budget values (overridable via env for power users, but defaults are computed)
THINKING_DISABLE_THRESHOLD = MAX_OUTPUT  # when output budget drops below this, disable thinking
SAFETY_MARGIN = _env_int("CTXGATE_SAFETY_MARGIN", int(MAX_CONTEXT * 0.05))
MAX_INPUT = _env_int("CTXGATE_MAX_INPUT", MAX_CONTEXT - SAFETY_MARGIN - MAX_OUTPUT)

# --- Rolling window (sticky cut) config ---
TRIM_TARGET_TOKENS = _env_int("CTXGATE_TRIM_TARGET_TOKENS", 0)
TRIM_TARGET_FRACTION = _env_float("CTXGATE_TRIM_TARGET_FRACTION", 0.70)
TRIM_TARGET_FLOOR = _env_int("CTXGATE_TRIM_TARGET_FLOOR", 20000)
SUMMARY_BACKFILL_CHARS = _env_int("CTXGATE_SUMMARY_BACKFILL_CHARS", 96000)
KNOWLEDGE_EXTRACT_MIN_CHARS = _env_int("CTXGATE_KNOWLEDGE_EXTRACT_MIN_CHARS", 40)
SUMMARY_CHUNK_CHARS = _env_int("CTXGATE_SUMMARY_CHUNK_CHARS", 24000)
SUMMARY_MAX_CHUNKS = _env_int("CTXGATE_SUMMARY_MAX_CHUNKS", 4)
SUMMARY_TOOL_CAP_CHARS = _env_int("CTXGATE_SUMMARY_TOOL_CAP_CHARS", 1000)
WINDOW_TTL_DAYS = _env_int("CTXGATE_WINDOW_TTL_DAYS", 7)
SSE_HEARTBEAT_INTERVAL = _env_int("CTXGATE_SSE_HEARTBEAT_INTERVAL", 10)
STABLE_ELIDE = os.environ.get("CTXGATE_STABLE_ELIDE", "1") != "0"

CTXGATE_PREFIX_DIAG_ON = os.environ.get("CTXGATE_PREFIX_DIAG", "0") == "1"
CTXGATE_INJECT_EPOCH_FREEZE_ON = os.environ.get("CTXGATE_INJECT_EPOCH_FREEZE", "1") == "1"
CTXGATE_INJECT_MAX_TOKENS_CFG = _env_int("CTXGATE_INJECT_MAX_TOKENS", 3000)
CTXGATE_INJECT_MAX_TOKENS_RECAP_CFG = _env_int("CTXGATE_INJECT_MAX_TOKENS_RECAP", 5000)
# High/low watermark for rolling window re-cut
CTXGATE_WINDOW_TRIGGER_FRAC = _env_float("CTXGATE_WINDOW_TRIGGER_FRAC", 0.65)
CTXGATE_WINDOW_TARGET_FRAC = _env_float("CTXGATE_WINDOW_TARGET_FRAC", 0.55)
CUT_QUANTUM = _env_int("CTXGATE_CUT_QUANTUM", 512)

WALL_CLOCK_MAX = _env_int("CTXGATE_WALL_CLOCK_MAX", 1800)
MAX_CONTINUATIONS = int(os.environ.get("CTXGATE_MAX_CONTINUATIONS", 5))
WORKER_BACKPRESSURE = _env_int("CTXGATE_WORKER_BACKPRESSURE", 50)
CTXGATE_MAX_TOTAL_OUTPUT = _env_int("CTXGATE_MAX_TOTAL_OUTPUT", 50000)
CTXGATE_MIN_CONTINUATION_OUTPUT = _env_int("CTXGATE_MIN_CONTINUATION_OUTPUT", 1024)
SESSION_TTL_HOURS = _env_int("CTXGATE_SESSION_TTL_HOURS", 12)
MEMORY_TTL_DAYS = _env_int("CTXGATE_MEMORY_TTL_DAYS", 90)

# --- Database & auth ---
DB_DSN = os.environ.get("CTXGATE_DB_DSN") or os.environ.get("CTXPROXY_DB_DSN") or ""
PROXY_PORT = _env_int("CTXGATE_PROXY_PORT", 9201)
API_KEY = os.environ.get("CTXGATE_API_KEY", "")
CTXGATE_HOST = os.environ.get("CTXGATE_HOST", "127.0.0.1")
CTXGATE_LM_ENABLED = os.environ.get("CTXGATE_LM_ENABLED", "1") != "0"
CTXGATE_GOOSE_INTEGRATION = os.environ.get("CTXGATE_GOOSE_INTEGRATION", "0") == "1"
CTXGATE_GOOSE_SESSION_RESOLVE = os.environ.get(
    "CTXGATE_GOOSE_SESSION_RESOLVE", "0") == "1"
CTXGATE_GOOSE_SESSION_CACHE_TTL = _env_float(
    "CTXGATE_GOOSE_SESSION_CACHE_TTL", 60.0)
CTXGATE_GOOSE_SESSION_LOOKUP_WINDOW_SEC = _env_int(
    "CTXGATE_GOOSE_SESSION_LOOKUP_WINDOW_SEC", 600)
CTXGATE_GOOSE_SESSION_CACHE_MAX = _env_int(
    "CTXGATE_GOOSE_SESSION_CACHE_MAX", 2000)
GOOSE_SESSION_UUID_NAMESPACE = uuid.UUID(
    "6ba7b810-9dad-11d1-80b4-00c04fd430c8")
CTXGATE_SESSION_IDENTITY = os.environ.get("CTXGATE_SESSION_IDENTITY", "goose")
CTXGATE_BLOCK_UNICODE_ESCAPES = os.environ.get("CTXGATE_BLOCK_UNICODE_ESCAPES", "0") == "1"
CTXGATE_TEST_ENDPOINTS = os.environ.get("CTXGATE_TEST_ENDPOINTS", "0") == "1"
CTXGATE_ALLOW_NO_AUTH = os.environ.get("CTXGATE_ALLOW_NO_AUTH", "0") == "1"
CTXGATE_ADMIN_KEY = os.environ.get("CTXGATE_ADMIN_KEY", "")
CTXGATE_API_KEYS = os.environ.get("CTXGATE_API_KEYS", "")
CTXGATE_TENANT_RATE_PER_SEC = _env_float("CTXGATE_TENANT_RATE_PER_SEC", 0.0)
CTXGATE_TENANT_RATE_BURST = _env_int("CTXGATE_TENANT_RATE_BURST", 0)
CTXGATE_TENANT_MAX_CONCURRENCY = _env_int("CTXGATE_TENANT_MAX_CONCURRENCY", 0)

MAX_BODY_BYTES = _env_int("CTXGATE_MAX_BODY_BYTES", 20 * 1024 * 1024)
QWEN_TOKENIZER_PATH = os.environ.get("CTXGATE_QWEN_TOKENIZER", "")

# --- Repetition-loop guard (streaming path) ---
LOOP_REPEATS = _env_int("CTXGATE_LOOP_REPEATS", 4)
LOOP_SENTENCE_REPEATS = _env_int("CTXGATE_LOOP_SENTENCE_REPEATS", 6)
LOOP_TAIL = 4000
LOOP_CHECK_EVERY = 256
MAX_REASONING_TOKENS = _env_int("CTXGATE_MAX_REASONING_TOKENS", 12000)
REPAIR_DANGLING_TOOLCALLS = _env_int("CTXGATE_REPAIR_DANGLING_TOOLCALLS", 1)
BLOCK_UNBOUNDED_LOOPS = _env_int("CTXGATE_BLOCK_UNBOUNDED_LOOPS", 1)
LOOP_RETRIES = _env_int("CTXGATE_LOOP_RETRIES", 1)
RETRY_TEMPERATURE = _env_float("CTXGATE_RETRY_TEMPERATURE", 0.7)
RETRY_TOP_P = _env_float("CTXGATE_RETRY_TOP_P", 0.8)
RETRY_PRESENCE_PENALTY = _env_float("CTXGATE_RETRY_PRESENCE_PENALTY", 1.5)
MIN_TEMPERATURE = _env_float("CTXGATE_MIN_TEMPERATURE", 0.3)
PRESENCE_PENALTY = os.environ.get("CTXGATE_PRESENCE_PENALTY", "")
REPETITION_DETECTION = os.environ.get("CTXGATE_REPETITION_DETECTION", "")
THINKING_TOKEN_BUDGET = os.environ.get("CTXGATE_THINKING_TOKEN_BUDGET", "")

# --- LM (Mistral) config ---
MISTRAL_URL = os.environ.get("CTXGATE_LM_URL", "https://api.mistral.ai/v1").removesuffix("/chat/completions")
MISTRAL_MODEL = os.environ.get("CTXGATE_LM_MODEL", "mistral-small-latest")
MISTRAL_TIMEOUT = _env_int("CTXGATE_LM_TIMEOUT", 120)
MISTRAL_API_KEY = os.environ.get("CTXGATE_LM_API_KEY", "")

MISTRAL_SYSTEM_PROMPT = """You are the durable-memory worker for a long-running software engineering agent.

Your only job is to extract and maintain information that the main 27B agent will need after the current conversation context is no longer available.

Read the current task state and the new event. Preserve only durable, useful information: confirmed decisions, important findings, failed approaches, constraints, important TODOs, important files, state changes, and stable facts. Do not solve the task, do not execute tools, do not invent information, and do not repeat information that is already known unless the new event corrects or supersedes it.

Prefer precise factual statements over summaries or explanations. Never guess. Only use information explicitly present in the input. When a new fact contradicts an existing memory, mark the old information as superseded through the requested memory action. When nothing important changed, return no memory changes.

Return ONLY valid JSON matching this exact schema. No Markdown. No commentary. No explanation outside the JSON.

Schema:
{
  "memory_actions": [
    {
      "action": "NEW" | "UPDATE" | "SUPERSEDE" | "DUPLICATE" | "NO_CHANGE",
      "type": "DECISION" | "FINDING" | "FAILURE" | "TODO" | "CONSTRAINT" | "FILE" | "STATE" | "FACT",
      "importance": "CRITICAL" | "HIGH" | "NORMAL" | "LOW",
      "title": "short title",
      "content": "the fact",
      "source_event_id": "string"
    }
  ],
  "state_update": {
    "changed": true | false,
    "current_state": "brief current state" | null,
    "current_subtask": "current subtask" | null
  }
}"""

# --- LM priority queue ---
LM_PRI_HIGH = 0   # trim summaries (feeds working memory into prompt)
LM_PRI_LOW = 2    # knowledge extraction (can wait 1 hour)

# --- Goose sessions DB ---
GOOSE_SESSIONS_DB = os.environ.get("GOOSE_SESSIONS_DB") or os.path.join(
    os.path.expanduser("~"), ".local", "share", "goose", "sessions", "sessions.db")

# === 4-Step Orchestrator Prompts ===

_PROMPT_EXTRACT = (
    "Extract concrete facts from the text below. "
    "Output JSON only with this structure: "
    "{\"state_update\": {\"changed\": true/false, \"current_state\": \"brief state\", \"current_subtask\": \"current task\"}, "
    "\"memory_actions\": [{\"action\": \"NEW\", \"type\": \"FACT\", \"importance\": \"NORMAL\", \"title\": \"short title\", \"content\": \"the fact\"}]} "
    "Extract: project names, tech stack, file paths, decisions, constraints, errors, config values, user preferences. "
    "Only extract what is explicitly stated. Never invent. If no new facts, use empty array for memory_actions."
)

_PROMPT_QUALITY = (
    "Review the proposed memory extraction. Check for hallucination (facts not in the original event) or broken JSON format. "
    "If ok, return: {\"pass\": true, \"reason\": \"clean\"} "
    "If hallucinated or malformed, return: {\"pass\": false, \"reason\": \"why\"} "
    "Output JSON only."
)

_PROMPT_DECIDE = (
    "Should these extracted memories be stored? They come from a real conversation event. "
    "If the facts are concrete and stated in the event: {\"store\": true} "
    "If empty, meaningless, or fully duplicated: {\"store\": false, \"reason\": \"why\"} "
    "Prefer storing. Output JSON only."
)

_IMPORTANCE_MAP = {"CRITICAL": 10, "HIGH": 8, "NORMAL": 5, "LOW": 3}

# --- Token estimator constants ---
RHO_COLD = 6.5         # chars/token from D6 measurement (Qwen3.8-27B)
RHO_LO = 3.0           # p3 of observed rho (dense content floor)
RHO_HI = 10.0          # p97 of observed rho (sparse content ceiling)
K_TOOLS_COLD = 1.537   # chars per token for tool definitions (conservative)
EWMA_ALPHA = 0.5       # per-session EWMA rate
GLOBAL_ALPHA = 0.1     # global EWMA rate
M_FLOOR = 0.08         # steady-state margin floor
M_FLOOR_COLD = 0.50    # cold-start margin floor (first 2 obs)
M_CAP = 0.25           # max margin
ESTIMATOR_MODE = os.environ.get("CTXGATE_TOKEN_ESTIMATOR", "calibrated")
CTXGATE_INPUT_ESTIMATE_HEADROOM = _env_float("CTXGATE_INPUT_ESTIMATE_HEADROOM", 0.05)
CTXGATE_PROACTIVE_TRIM_FRAC = _env_float("CTXGATE_PROACTIVE_TRIM_FRAC", 0.80)

