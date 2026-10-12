"""LM (Mistral) queue consumer: pulls tasks and executes them."""
import asyncio
import logging
import time

from proxy.config.constants import LM_PRI_LOW
from proxy.lm.call import _lm_do_call
import re

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


async def _lm_consumer(worker_id: int = 0):
    """Independent worker: pull task, execute, resolve future. No cross-blocking."""
    # TODO(I4): replace with AppContext
    from proxy.app import _lm_queue
    global _lm_client
    while True:
        priority, seq, task = await _lm_queue.get()
        # Time-based aging: P2 tasks waiting > 120s bump to priority 1
        if priority == LM_PRI_LOW:
            age = time.time() - task.get("enqueued_at", 0)
            if age > 120:
                priority = 1
        try:
            result = await _lm_do_call(task)
            if not task["future"].done():
                task["future"].set_result(result)
        except asyncio.CancelledError:
            if not task["future"].done():
                task["future"].cancel()
            raise
        except Exception as e:
            log.warning("Mistral worker-%d task failed (pri=%d seq=%d): %s",
                        worker_id, priority, seq, e)
            if not task["future"].done():
                task["future"].set_result({} if task["json_mode"] else "")
        finally:
            _lm_queue.task_done()

