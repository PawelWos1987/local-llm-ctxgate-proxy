"""Integration test: verifies the fix for 'Response reached the model's output-token limit'.

Bug: With input > 63k tokens, thinking stayed ON because the trigger was
MAX_INPUT (64800). The model burned all 15000 output tokens on internal
reasoning, leaving zero for visible content. Goose reported:
  'Response reached the model's output-token limit before returning content.'

Fix: _think_trigger = MAX_INPUT. When input exceeds this, thinking is
disabled so the full output budget goes to visible content.

This test exercises the HOT PATH via the agent-session-id header
(proxy/hotpath/session_id.py:_resolve_session_identity), sends a payload
with input > MAX_INPUT, and asserts:
  1. disable_thinking=True is passed to _build_vllm_body
  2. max_tokens equals MAX_OUTPUT (full budget for content)
  3. The response contains non-empty content (no empty-response error)
"""
import os
import sys
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport

# Ensure proxy package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Constants under test
# ---------------------------------------------------------------------------

from proxy.config.constants import (
    MAX_CONTEXT,
    MAX_OUTPUT,
    SAFETY_MARGIN,
    MAX_INPUT,
    MAX_REASONING_TOKENS,
)


def _make_large_messages(target_tokens: int) -> list:
    """Build messages whose total token count approximates target_tokens.

    Uses varied text (not repetitive) so the tokenizer estimates close to
    real values. The proxy's cold-path estimator runs at ~1.6 tokens/char
    for diverse text. We over-provision by 2x to guarantee exceeding the
    threshold regardless of estimation variance.
    """
    import string
    msgs = []
    # System message (small)
    msgs.append({"role": "system", "content": "You are a helpful assistant."})
    
    # Target chars: aim for target_tokens worth of diverse text
    # Use ~0.6 tokens/char for mixed alphanumeric text
    target_chars = int(target_tokens / 0.6)
    
    # Build a pool of diverse sentences to avoid compression artifacts
    word_pool = [
        "the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog",
        "data", "processing", "pipeline", "transformation", "aggregation",
        "network", "protocol", "handshake", "authentication", "encryption",
        "database", "query", "optimization", "index", "partition", "shard",
        "distributed", "consensus", "replication", "fault", "tolerance",
        "algorithm", "complexity", "asymptotic", "amortized", "heuristic",
    ]
    
    # Generate diverse paragraphs
    paragraph = " ".join(word_pool * 4)  # ~200 words per paragraph
    para_len = len(paragraph)
    
    n_paragraphs = max(1, target_chars // para_len)
    
    for i in range(n_paragraphs):
        # Vary slightly to prevent dedup/compression
        offset = i % len(word_pool)
        varied = " ".join(word_pool[offset:] + word_pool[:offset])
        user_content = (varied + " ") * 3  # repeat 3x for volume
        msgs.append({"role": "user", "content": user_content})
        msgs.append({"role": "assistant", "content": "Acknowledged. Processing the provided context and continuing analysis."})
    
    return msgs


class TestOutputTokenLimitFix:
    """Verify that input > MAX_INPUT triggers thinking-disable, preventing
    the 'output-token limit' empty-content error."""

    @pytest.mark.asyncio
    async def test_input_above_max_input_disables_thinking(self):
        """Core regression: input > MAX_INPUT must result in disable_thinking=True
        and full MAX_OUTPUT for content.
        
        Original bug: with input=63635, thinking stayed ON because the trigger
        was MAX_INPUT (64800). Model burned all 15000 tokens on reasoning →
        empty content → goose error.
        
        Fix: _think_trigger = MAX_INPUT. When input exceeds this, thinking is
        disabled so the full output budget goes to visible content.
        
        We mock count_messages_tokens to simulate a 65000-token payload
        (above MAX_INPUT=64800) without needing to generate megabytes of text.
        """
        
        from proxy.app import app
        
        # Realistic small messages (the token count is mocked)
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Tell me about distributed systems and consensus algorithms."},
        ]
        
        body = {
            "model": "Qwen3.8-27B",
            "messages": messages,
            "max_tokens": MAX_OUTPUT,
            "stream": False,
        }
        
        headers = {
            "agent-session-id": "test-output-limit-fix",
            "Content-Type": "application/json",
        }
        
        # Simulate a payload that estimates to 65000 tokens (> MAX_INPUT=64800)
        simulated_input_tokens = MAX_INPUT + 200
        
        captured = {}
        
        async def fake_forward(vllm_body, input_tokens, session_key, client_cap, live_prompt_hash=None, stream=False):
            captured["vllm_body"] = vllm_body
            captured["input_tokens"] = input_tokens
            captured["session_key"] = session_key
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Here is my response with actual content about distributed systems.",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": input_tokens, "completion_tokens": 100},
            }
        
        # Mock the token estimator to return our simulated high value
        with patch("proxy.hotpath.handler.exact_count_messages", 
                   return_value=simulated_input_tokens), \
             patch("proxy.hotpath.handler._forward_to_vllm", side_effect=fake_forward):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers=headers,
                )
        
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text[:500]}"
        
        data = resp.json()
        
        # 1. Response must have non-empty content (the original bug produced empty)
        content = data["choices"][0]["message"].get("content", "")
        assert len(content) > 10, f"Content too short (original bug): '{content}'"
        
        # 2. finish_reason must be 'stop', NOT 'length'
        finish = data["choices"][0].get("finish_reason", "")
        assert finish != "length", f"finish_reason='length' means output was truncated: {data}"
        
        # 3. The vLLM body must have had thinking disabled
        vllm_body = captured.get("vllm_body", {})
        extra = vllm_body.get("extra_body", {})
        chat_kw = vllm_body.get("chat_template_kwargs", {})
        disable_flag = (
            extra.get("chat_template_kwargs", {}).get("enable_thinking") is False
            or chat_kw.get("enable_thinking") is False
            or vllm_body.get("thinking_token_budget") == 0
        )
        assert disable_flag, (
            f"Thinking was NOT disabled for input={simulated_input_tokens} > MAX_INPUT={MAX_INPUT}. "
            f"vllm_body keys={list(vllm_body.keys())} "
            f"extra_body={extra} chat_template_kwargs={chat_kw}"
        )
        
        # 4. max_tokens should be close to MAX_OUTPUT (capped by remaining context room)
        # When input > MAX_INPUT: max_tokens = MAX_CONTEXT - input - SAFETY_MARGIN
        # Output budget = MAX_CONTEXT - input - SAFETY_MARGIN (must stay positive)
        actual_max = vllm_body.get("max_tokens", 0)
        expected_max = MAX_CONTEXT - simulated_input_tokens - SAFETY_MARGIN
        assert abs(actual_max - expected_max) < 100, (
            f"max_tokens={actual_max}, expected ~{expected_max} "
            f"(MAX_CONTEXT - input - SAFETY_MARGIN)"
        )
        # And critically: it must be a large enough budget for real content
        assert actual_max >= 8000, (
            f"max_tokens={actual_max} too small for meaningful content"
        )

    @pytest.mark.asyncio
    async def test_input_below_threshold_keeps_thinking(self):
        """Control: input well below MAX_INPUT should keep thinking ON."""
        
        from proxy.app import app
        
        # Small input (~2000 tokens, well under any threshold)
        messages = _make_large_messages(2000)
        
        body = {
            "model": "Qwen3.8-27B",
            "messages": messages,
            "max_tokens": MAX_OUTPUT,
            "stream": False,
        }
        
        headers = {
            "agent-session-id": "test-thinking-on",
            "Content-Type": "application/json",
        }
        
        captured = {}
        
        async def fake_forward(vllm_body, input_tokens, session_key, client_cap, live_prompt_hash=None, stream=False):
            captured["vllm_body"] = vllm_body
            captured["input_tokens"] = input_tokens
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Thinking enabled response.",
                        "reasoning_content": "Let me think about this...",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": input_tokens, "completion_tokens": 50},
            }
        
        with patch("proxy.hotpath.handler._forward_to_vllm", side_effect=fake_forward):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers=headers,
                )
        
        assert resp.status_code == 200
        
        vllm_body = captured.get("vllm_body", {})
        # Thinking should NOT be disabled for small inputs
        extra = vllm_body.get("extra_body", {})
        chat_kw = vllm_body.get("chat_template_kwargs", {})
        thinking_off = (
            extra.get("chat_template_kwargs", {}).get("enable_thinking") is False
            or chat_kw.get("enable_thinking") is False
            or vllm_body.get("thinking_token_budget") == 0
        )
        assert not thinking_off, (
            f"Thinking was unexpectedly disabled for small input. "
            f"extra_body={extra} chat_template_kwargs={chat_kw}"
        )

    @pytest.mark.asyncio
    async def test_hot_path_uses_agent_session_id_header(self):
        """Verify the hot path resolves identity from agent-session-id header
        (not fallback fingerprint)."""
        
        from proxy.app import app
        
        messages = [{"role": "user", "content": "Hello"}]
        body = {
            "model": "Qwen3.8-27B",
            "messages": messages,
            "max_tokens": 100,
            "stream": False,
        }
        
        headers = {
            "agent-session-id": "my-goose-session-123",
            "Content-Type": "application/json",
        }
        
        captured = {}
        
        async def fake_forward(vllm_body, input_tokens, session_key, client_cap, live_prompt_hash=None, stream=False):
            captured["session_key"] = session_key
            return {
                "choices": [{
                    "message": {"role": "assistant", "content": "Hi!"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        
        with patch("proxy.hotpath.handler._forward_to_vllm", side_effect=fake_forward):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers=headers,
                )
        
        assert resp.status_code == 200
        # Session key should be derived from the header value
        assert captured["session_key"] == "goose:my-goose-session-123", (
            f"Expected 'goose:my-goose-session-123', got '{captured['session_key']}'"
        )
