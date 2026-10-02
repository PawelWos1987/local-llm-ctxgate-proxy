"""Suite 16: Streaming at the trim boundary."""
import asyncio
import json
import time
import urllib.request

import pytest

PROXY = "http://127.0.0.1:9201"


def _post_stream(path, body, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(PROXY + path, data=json.dumps(body).encode(), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        lines = []
        for raw in r:
            lines.append(raw.decode())
        return r.status, lines


def _make_oversized(n_chars=100000):
    """Create messages that exceed MAX_INPUT (64k tokens ~= 256k chars)."""
    filler = "lorem ipsum dolor sit amet " * (n_chars // 30 + 1)
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": filler},
    ]


def _make_boundary(n_chars=90000):
    """Create messages near the trim boundary."""
    filler = "boundary test content " * (n_chars // 23 + 1)
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": filler},
    ]


class TestStreamingAtCap:
    def test_sc1_nonstream_oversized(self):
        """Non-stream oversized payload gets trimmed, returns 200."""
        body = {
            "model": "Qwen3.8-27B",
            "messages": _make_oversized(),
            "max_tokens": 16,
            "stream": False,
        }
        req = urllib.request.Request(
            PROXY + "/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            assert r.status == 200
            data = json.loads(r.read())
            assert "choices" in data

    def test_sc2_stream_oversized(self):
        """Streaming oversized payload gets trimmed, valid SSE."""
        body = {
            "model": "Qwen3.8-27B",
            "messages": _make_oversized(),
            "max_tokens": 16,
            "stream": True,
        }
        status, lines = _post_stream("/v1/chat/completions", body)
        assert status == 200
        # Must have data lines and DONE
        data_lines = [l for l in lines if l.startswith("data: ")]
        assert len(data_lines) >= 2, f"Expected >=2 data lines, got {len(data_lines)}"
        assert any(l.strip() == "data: [DONE]" for l in data_lines)

    def test_sc3_stream_boundary(self):
        """Streaming at trim boundary (~64k tokens)."""
        body = {
            "model": "Qwen3.8-27B",
            "messages": _make_boundary(),
            "max_tokens": 16,
            "stream": True,
        }
        status, lines = _post_stream("/v1/chat/completions", body)
        assert status == 200
        data_lines = [l for l in lines if l.startswith("data: ")]
        assert len(data_lines) >= 2

    def test_sc4_concurrent_stream_cap(self):
        """3 concurrent streaming requests at cap - all succeed."""
        def do_one(topic):
            msgs = [
                {"role": "system", "content": f"You are a {topic} expert."},
                {"role": "user", "content": "test " * 10000},
            ]
            body = {
                "model": "Qwen3.8-27B",
                "messages": msgs,
                "max_tokens": 8,
                "stream": True,
            }
            return _post_stream("/v1/chat/completions", body)

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(do_one, t) for t in ["stream1", "stream2", "stream3"]]
            results = [f.result() for f in futures]

        assert all(r[0] == 200 for r in results), f"Some failed: {[r[0] for r in results]}"
        for status, lines in results:
            data_lines = [l for l in lines if l.startswith("data: ")]
            assert any(l.strip() == "data: [DONE]" for l in data_lines)