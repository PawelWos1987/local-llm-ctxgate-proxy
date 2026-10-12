"""Memory subsystem: task memory, knowledge fetching, scoring, extraction, storage, jobs."""
from proxy.memory.fetch import fetch_task_memory, fetch_relevant_knowledge
from proxy.memory.score import _score_memory, _extract_terms, _context_blob, _already_in_context
from proxy.memory.extract import extract_knowledge, _fire_and_forget_extract
from proxy.memory.store import store_knowledge
from proxy.memory.job import _enqueue_memory_job, _bg_ensure_task_and_enqueue

__all__ = [
    "fetch_task_memory", "fetch_relevant_knowledge",
    "_score_memory", "_extract_terms", "_context_blob", "_already_in_context",
    "extract_knowledge", "_fire_and_forget_extract",
    "store_knowledge",
    "_enqueue_memory_job", "_bg_ensure_task_and_enqueue",
]
