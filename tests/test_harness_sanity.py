"""Sanity: asyncio.run works under pytest without any plugin."""

import asyncio


def test_sanity_asyncio_runs():
    async def _coro():
        await asyncio.sleep(0)
        return 42

    assert asyncio.run(_coro()) == 42


def test_harness_drives_a_pure_text_stream():
    from tests.fakes import run_stream, script_pure_text_normal
    events = run_stream([script_pure_text_normal()])
    kinds = [e["kind"] for e in events]
    assert "chunk" in kinds
    assert kinds[-1] == "done"
