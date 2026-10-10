#!/usr/bin/env python3
"""Deliverable endpoint tests for ctxgate-proxy.

Tests POST /deliverable (full + auto-enrichment), GET /deliverable,
PATCH /deliverable/{id}, and dashboard rendering.

Run against a SEPARATE instance on port 9209 (never the live 9201).
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

import pytest
pytestmark = pytest.mark.skip(reason="requires live proxy on port 9209")

PROXY = os.environ.get("TEST_PROXY_URL", "http://127.0.0.1:9209")
DB_DSN = os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy")

passed = 0
failed = 0
results = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        results.append(("PASS", name))
        print(f"  PASS: {name}")
    else:
        failed += 1
        results.append(("FAIL", name + " " + detail))
        print(f"  FAIL: {name} {detail}")


def http_post(url, data, headers=None):
    body = json.dumps(data).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def http_get(url, headers=None):
    hdrs = {}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def http_patch(url, data, headers=None):
    body = json.dumps(data).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def cleanup_deliverables(names):
    """Delete test rows from proxy.deliverables by name."""
    try:
        import asyncpg
        import asyncio

        async def _delete():
            conn = await asyncpg.connect(DB_DSN)
            for name in names:
                await conn.execute(
                    "DELETE FROM proxy.deliverables WHERE name=$1", name
                )
            await conn.close()

        asyncio.run(_delete())
    except Exception as e:
        print(f"  WARN: cleanup failed: {e}")


# ============================================================
# Test 1: POST /deliverable with full body
# ============================================================
def test_post_full():
    print("\n== T1: POST /deliverable (full body) ==")
    name = f"test-full-{uuid.uuid4().hex[:8]}"
    s, body = http_post(
        PROXY + "/deliverable",
        {
            "name": name,
            "session_type": "goose",
            "working_dir": "/tmp/ctxproxy-test-project",
            "provider_name": "mistral",
            "summary": "Full body test deliverable",
            "document_path": "/tmp/ctxproxy-scratch/test-full.md",
        },
    )
    check("T1_status_200", s == 200, f"got {s}: {body[:200]}")
    if s == 200:
        d = json.loads(body)
        check("T1_has_id", "id" in d and len(d["id"]) > 0)
        check("T1_name", d.get("name") == name, f"got {d.get('name')}")
        check("T1_session_type", d.get("session_type") == "goose")
        check("T1_working_dir", d.get("working_dir") == "/tmp/ctxproxy-test-project")
        check("T1_provider", d.get("provider_name") == "mistral")
        check("T1_summary", d.get("summary") == "Full body test deliverable")
        check("T1_doc_path", d.get("document_path") == "/tmp/ctxproxy-scratch/test-full.md")
        check("T1_created_at", "created_at" in d)
        return d.get("id")
    return None


# ============================================================
# Test 2: POST /deliverable with partial body (auto-enrichment)
# ============================================================
def test_post_partial():
    print("\n== T2: POST /deliverable (partial body, auto-enrichment) ==")
    name = f"test-partial-{uuid.uuid4().hex[:8]}"
    # Use X-Session-ID header pointing to a real session if available
    # The auto-enrichment will try to fill from Goose sessions DB
    s, body = http_post(
        PROXY + "/deliverable",
        {
            "name": name,
            "document_path": "/tmp/ctxproxy-scratch/test-partial.md",
        },
        headers={"X-Session-ID": "test-partial-session"},
    )
    check("T2_status_200", s == 200, f"got {s}: {body[:200]}")
    if s == 200:
        d = json.loads(body)
        check("T2_has_id", "id" in d and len(d["id"]) > 0)
        check("T2_name", d.get("name") == name)
        # session_type should default to "goose" if not found in sessions DB
        check("T2_session_type_set", d.get("session_type") is not None and len(d.get("session_type", "")) > 0,
              f"got '{d.get('session_type')}'")
        check("T2_doc_path", d.get("document_path") == "/tmp/ctxproxy-scratch/test-partial.md")
        return d.get("id")
    return None


# ============================================================
# Test 3: GET /deliverable?limit=5
# ============================================================
def test_get_deliverables():
    print("\n== T3: GET /deliverable?limit=5 ==")
    s, body = http_get(PROXY + "/deliverable?limit=5")
    check("T3_status_200", s == 200, f"got {s}")
    if s == 200:
        d = json.loads(body)
        check("T3_has_data", "data" in d)
        check("T3_data_is_list", isinstance(d.get("data"), list))
        check("T3_max_5", len(d.get("data", [])) <= 5)
        if d.get("data"):
            first = d["data"][0]
            check("T3_has_fields", all(k in first for k in ["id", "name", "session_type", "working_dir", "provider_name", "summary", "document_path", "created_at", "updated_at"]))


# ============================================================
# Test 4: PATCH /deliverable/{id}
# ============================================================
def test_patch(deliverable_id):
    print("\n== T4: PATCH /deliverable/{id} ==")
    if not deliverable_id:
        check("T4_skip_no_id", False, "No deliverable ID from T1")
        return

    # Patch summary
    s, body = http_patch(
        PROXY + f"/deliverable/{deliverable_id}",
        {"summary": "Updated summary via PATCH"},
    )
    check("T4_patch_summary_200", s == 200, f"got {s}: {body[:200]}")
    if s == 200:
        d = json.loads(body)
        check("T4_summary_updated", d.get("summary") == "Updated summary via PATCH")
        check("T4_has_updated_at", "updated_at" in d)

    # Patch name
    s2, body2 = http_patch(
        PROXY + f"/deliverable/{deliverable_id}",
        {"name": "renamed-deliverable"},
    )
    check("T4_patch_name_200", s2 == 200, f"got {s2}")
    if s2 == 200:
        d2 = json.loads(body2)
        check("T4_name_updated", d2.get("name") == "renamed-deliverable")

    # Patch with bad id -> 404
    s3, body3 = http_patch(
        PROXY + "/deliverable/00000000-0000-0000-0000-000000000000",
        {"summary": "should not work"},
    )
    check("T4_bad_id_404", s3 == 404, f"got {s3}: {body3[:200]}")


# ============================================================
# Main
# ============================================================
def main():
    global passed, failed
    print(f"\n{'='*60}")
    print(f"Deliverable endpoint tests")
    print(f"Proxy: {PROXY}")
    print(f"{'='*60}")

    test_names = []

    # T1
    id1 = test_post_full()
    if id1:
        test_names.append(f"test-full-{id1[:8]}")

    # T2
    id2 = test_post_partial()

    # T3
    test_get_deliverables()

    # T4
    test_patch(id1)

    # Cleanup
    print("\n== Cleanup ==")
    # Clean up by name pattern
    try:
        import asyncpg
        import asyncio

        async def _cleanup():
            conn = await asyncpg.connect(DB_DSN)
            await conn.execute(
                "DELETE FROM proxy.deliverables WHERE name LIKE 'test-full-%' OR name LIKE 'test-partial-%' OR name = 'renamed-deliverable'"
            )
            await conn.close()

        asyncio.run(_cleanup())
        print("  Cleanup done")
    except Exception as e:
        print(f"  WARN: cleanup failed: {e}")

    # Summary
    print(f"\n{'='*60}")
    print(f"RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
    print(f"{'='*60}")
    for status, name in results:
        print(f"  {status}: {name}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
