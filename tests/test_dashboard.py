"""Suite 17: Web dashboard and API endpoints."""
import json
import urllib.request

import pytest

PROXY = "http://127.0.0.1:9200"


def _get(path):
    req = urllib.request.Request(PROXY + path)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()


def _post(path, body, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(PROXY + path, data=json.dumps(body).encode(), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture(autouse=True)
def reset():
    yield
    _post("/_test/reset_sessions", {})


class TestDashboard:
    def test_d1_dashboard_html(self):
        """Dashboard returns 200 + text/html."""
        s, body = _get("/dashboard")
        assert s == 200
        assert "text/html" in body or "<!DOCTYPE" in body
        assert "local-llm-ctxgate-proxy" in body
        assert "Chart" in body or "chart" in body

    def test_d2_api_metrics(self):
        """API metrics returns valid JSON with expected fields."""
        s, body = _get("/api/metrics")
        assert s == 200
        m = json.loads(body)
        assert "requests_total" in m
        assert "tokens_in_total" in m
        assert "uptime_human" in m
        assert "started_human" in m
        assert "active_sessions" in m

    def test_d3_api_sessions(self):
        """API sessions returns array."""
        s, body = _get("/api/sessions")
        assert s == 200
        sessions = json.loads(body)
        assert isinstance(sessions, list)

    def test_d4_api_recent_calls(self):
        """API recent-calls returns array."""
        s, body = _get("/api/recent-calls?n=10")
        assert s == 200
        calls = json.loads(body)
        assert isinstance(calls, list)

    def test_d5_api_memory_summary(self):
        """API memory-summary returns object."""
        s, body = _get("/api/memory-summary")
        assert s == 200
        m = json.loads(body)
        assert "tasks" in m
        assert "memory_entries" in m

    def test_d6_metrics_human_fields(self):
        """/metrics has human-readable time fields."""
        s, body = _get("/metrics")
        assert s == 200
        m = json.loads(body)
        assert "uptime_human" in m
        assert "started_human" in m
        assert "active_sessions" in m

    def test_d7_sessions_after_requests(self):
        """After 2 requests, sessions has 2 entries with correct tokens."""
        _post("/v1/chat/completions", {
            "model": "Qwen3.8-27B",
            "messages": [
                {"role": "system", "content": "You are a dash test bot."},
                {"role": "user", "content": "Hello dash test."},
            ],
            "max_tokens": 8,
        }, {"X-Session-ID": "dashtest"})

        _post("/v1/chat/completions", {
            "model": "Qwen3.8-27B",
            "messages": [
                {"role": "system", "content": "You are a different dash bot."},
                {"role": "user", "content": "Hello different."},
            ],
            "max_tokens": 8,
        }, {"X-Session-ID": "dashtest"})

        s, body = _get("/api/sessions")
        sessions = json.loads(body)
        assert len(sessions) == 2
        for s in sessions:
            assert s["tokens_in"] > 0
            assert s["requests"] >= 1

    def test_d8_ring_buffer_max(self):
        """Ring buffer caps at 200."""
        s, body = _get("/api/recent-calls?n=500")
        calls = json.loads(body)
        assert len(calls) <= 200