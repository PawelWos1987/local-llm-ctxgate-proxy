"""Suite 15: Session isolation - per-session prefix + token accounting."""
import json
import urllib.request

import pytest

PROXY = "http://127.0.0.1:9201"


def _post(path, body, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(PROXY + path, data=json.dumps(body).encode(), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _topic_messages(topic):
    return [
        {"role": "system", "content": f"You are a {topic} expert."},
        {"role": "user", "content": f"Tell me about {topic}."},
    ]


def _get_metric(name):
    req = urllib.request.Request(PROXY + "/metrics")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()).get(name, 0)


@pytest.fixture(autouse=True)
def reset_sessions():
    _post("/_test/reset_sessions", {})
    yield
    _post("/_test/reset_sessions", {})


class TestSessionIsolation:
    def test_s1_two_topics_separate(self):
        """Two different topics get different session keys, no cross-talk."""
        s1, r1 = _post("/v1/chat/completions", {
            "model": "Qwen3.8-27B", "messages": _topic_messages("quantum physics"),
            "max_tokens": 16,
        }, {"X-Session-ID": "test-provider"})
        assert s1 == 200

        s2, r2 = _post("/v1/chat/completions", {
            "model": "Qwen3.8-27B", "messages": _topic_messages("baking cakes"),
            "max_tokens": 16,
        }, {"X-Session-ID": "test-provider"})
        assert s2 == 200

        # Check sessions endpoint shows 2 separate sessions
        req = urllib.request.Request(PROXY + "/api/sessions")
        with urllib.request.urlopen(req, timeout=10) as r:
            sessions = json.loads(r.read())
        assert len(sessions) == 2, f"Expected 2 sessions, got {len(sessions)}"
        keys = [s["key"] for s in sessions]
        assert len(set(keys)) == 2, "Session keys must be unique"

    def test_s2_back_and_forth(self):
        """3 topics back-and-forth: A, B, C, A, B. Each keeps its own fp."""
        topics = ["algebra", "cooking", "music"]
        for i, t in enumerate(topics + topics[:2]):
            s, r = _post("/v1/chat/completions", {
                "model": "Qwen3.8-27B",
                "messages": _topic_messages(t),
                "max_tokens": 8,
            }, {"X-Session-ID": "baftest"})
            assert s == 200, f"Iter {i} topic={t} failed: {s}"

        req = urllib.request.Request(PROXY + "/api/sessions")
        with urllib.request.urlopen(req, timeout=10) as r:
            sessions = json.loads(r.read())
        assert len(sessions) == 3, f"Expected 3 sessions, got {len(sessions)}"

        # prefix_invalidations must NOT increase during this test (same topic revisited = same fp).
        # Use a baseline delta: the counter is process-global, so we compare against the value
        # captured by the autouse reset_sessions fixture (proxy state was reset before this test).
        assert _get_metric("prefix_invalidations") == 0, f"prefix_invalidations after reset != 0: {_get_metric('prefix_invalidations')}"

    def test_s3_per_session_tokens(self):
        """Per-session token accounting is correct."""
        for i in range(2):
            _post("/v1/chat/completions", {
                "model": "Qwen3.8-27B",
                "messages": _topic_messages("widgets"),
                "max_tokens": 8,
            }, {"X-Session-ID": "toktest"})

        req = urllib.request.Request(PROXY + "/api/sessions")
        with urllib.request.urlopen(req, timeout=10) as r:
            sessions = json.loads(r.read())
        assert len(sessions) == 1
        s = sessions[0]
        assert s["requests"] == 2
        assert s["tokens_in"] > 0
        assert s["tokens_out"] >= 0

    def test_s4_concurrent_topics(self):
        """4 concurrent different topics - no cross-contamination."""
        import concurrent.futures

        def call(topic):
            body = json.dumps({
                "model": "Qwen3.8-27B",
                "messages": _topic_messages(topic),
                "max_tokens": 8,
            }).encode()
            req = urllib.request.Request(
                PROXY + "/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json", "X-Session-ID": "conctest"},
                method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(call, t) for t in ["alpha", "bravo", "charlie", "delta"]]
            statuses = [f.result() for f in futures]

        assert all(s == 200 for s in statuses), f"Some failed: {statuses}"

        req = urllib.request.Request(PROXY + "/api/sessions")
        with urllib.request.urlopen(req, timeout=10) as r:
            sessions = json.loads(r.read())
        assert len(sessions) == 4, f"Expected 4 sessions, got {len(sessions)}"

    def test_s5_reset_sessions(self):
        """Reset endpoint clears all session state."""
        _post("/v1/chat/completions", {
            "model": "Qwen3.8-27B",
            "messages": _topic_messages("resetme"),
            "max_tokens": 8,
        }, {"X-Session-ID": "resettest"})

        s, r = _post("/_test/reset_sessions", {})
        assert s == 200

        req = urllib.request.Request(PROXY + "/api/sessions")
        with urllib.request.urlopen(req, timeout=10) as r:
            sessions = json.loads(r.read())
        assert len(sessions) == 0

    def test_s6_same_topic_revisit(self):
        """Same topic revisited = same key = no prefix invalidation."""
        msgs = _topic_messages("revisitme")
        for _ in range(3):
            s, r = _post("/v1/chat/completions", {
                "model": "Qwen3.8-27B",
                "messages": msgs,
                "max_tokens": 8,
            }, {"X-Session-ID": "revisit"})
            assert s == 200

        # After reset_sessions fixture, counter should be 0; 3 same-topic calls must not invalidate.
        assert _get_metric("prefix_invalidations") == 0, f"prefix_invalidations != 0: {_get_metric('prefix_invalidations')}"
