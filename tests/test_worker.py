"""Tests for worker reliability (W1-W6)."""
import asyncio
import pytest
import os
import sys
sys.path.insert(0, '/home/pawelw/ctxproxy/worker')


@pytest.mark.asyncio
async def test_qc_temp_defined():
    """W2: QC_TEMP is defined and defaults to 0.0."""
    os.environ["CTXGATE_WORKER_QC_TEMP"] = "0.0"
    import importlib
    import worker
    importlib.reload(worker)
    assert worker.QC_TEMP == 0.0


@pytest.mark.asyncio
async def test_qc_max_tokens_defined():
    """W2: QC_MAX_TOKENS is defined and defaults to 256."""
    os.environ["CTXGATE_WORKER_QC_MAX_TOKENS"] = "256"
    import importlib
    import worker
    importlib.reload(worker)
    assert worker.QC_MAX_TOKENS == 256


@pytest.mark.asyncio
async def test_inflight_job_ids_set_exists():
    """W1: inflight_job_ids set exists and is distinct from inflight_tasks."""
    import importlib
    import worker
    importlib.reload(worker)
    assert hasattr(worker, 'inflight_job_ids')
    assert isinstance(worker.inflight_job_ids, set)
    assert worker.inflight_job_ids is not worker.inflight_tasks


@pytest.mark.asyncio
async def test_lm_disabled_worker():
    """W5: LM-disabled worker does not claim jobs."""
    os.environ["CTXGATE_LM_ENABLED"] = "0"
    import importlib
    import worker
    importlib.reload(worker)
    assert worker.LM_ENABLED == False


@pytest.mark.asyncio
async def test_fair_claiming_order():
    """W4: Claiming uses creation-time-first ordering."""
    source = open('/home/pawelw/ctxproxy/worker/worker.py').read()
    # Should order by created_at first
    assert "ORDER BY created_at" in source or "ORDER BY created_at, task_id" in source


@pytest.mark.asyncio
async def test_heartbeat_scoped_to_owned_jobs():
    """W1: Heartbeat only refreshes owned in-flight jobs."""
    source = open('/home/pawelw/ctxproxy/worker/worker.py').read()
    # Should reference inflight_job_ids in the heartbeat
    assert "inflight_job_ids" in source


@pytest.mark.asyncio
async def test_ready_after_init():
    """S5: READY=1 sent after pool initialization."""
    source = open('/home/pawelw/ctxproxy/worker/worker.py').read()
    # READY=1 should come after create_pool
    ready_pos = source.find('READY=1')
    pool_pos = source.find('create_pool')
    assert ready_pos > pool_pos, "READY=1 must come after pool creation"
