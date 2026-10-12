"""Ledger entry conflict detection and supersession."""
import json

_CONFLICT_TOPICS = {
    "database": ["postgresql", "mysql", "mongodb", "sqlite", "postgres", "mariadb", "database", "orm", "redis"],
    "web_framework": ["fastapi", "django", "flask", "express", "svelte", "react", "vue", "nextjs", "web framework"],
    "language": ["python", "javascript", "typescript", "java", "rust", "go", "language", "c++", "c#"],
    "runtime": ["node", "deno", "bun", "runtime", "pyodide"],
    "api_protocol": ["rest", "graphql", "grpc", "websocket", "sse", "api protocol"],
    "architecture": ["microservice", "monolith", "pattern", "layer", "module", "component", "event-driven"],
    "deployment": ["docker", "kubernetes", "helm", "ci/cd", "pipeline", "deploy", "infrastructure", "aws", "gcp", "azure"],
    "testing": ["pytest", "unittest", "test framework", "coverage", "mock", "fixture"],
    "configuration": ["config", "setting", "environment variable", "flag", "option"],
    "security": ["auth", "token", "secret", "credential", "encryption", "tls", "ssl", "oauth"],
    "performance": ["cache", "optimization", "latency", "throughput", "benchmark", "profiling"],
}



def _entries_conflict(entry_a: dict, entry_b: dict) -> bool:
    """Determine if two ledger entries conflict (address the same topic with opposing content)."""
    text_a = (entry_a.get("text", "") + " " + entry_a.get("entry_type", "")).lower()
    text_b = (entry_b.get("text", "") + " " + entry_b.get("entry_type", "")).lower()
    
    for topic, keywords in _CONFLICT_TOPICS.items():
        a_has = any(kw in text_a for kw in keywords)
        b_has = any(kw in text_b for kw in keywords)
        if a_has and b_has:
            a_specifics = [kw for kw in keywords if kw in text_a]
            b_specifics = [kw for kw in keywords if kw in text_b]
            if a_specifics != b_specifics:
                return True
            if "never" in text_a or "don't" in text_a or "no " in text_a:
                if "use" in text_b or "must" in text_b:
                    return True
            if "never" in text_b or "don't" in text_b or "no " in text_b:
                if "use" in text_a or "must" in text_a:
                    return True
    return False





def _apply_supersession(entries: list, new_entry: dict) -> list:
    """A new explicit instruction supersedes only conflicting older ones.
    
    Rules:
    - A new EXPLICIT instruction supersedes conflicting older entries
    - A new NON-explicit (summary-derived) entry CANNOT supersede an explicit entry
    - Unrelated entries remain active
    - Superseded entries get status='superseded' and superseded_by=new_entry.id
    """
    if not new_entry.get("explicit", False):
        for e in entries:
            if e.get("status") != "active":
                continue
            if e.get("explicit", False):
                continue
            if _entries_conflict(e, new_entry):
                e["status"] = "superseded"
                e["superseded_by"] = new_entry.get("id")
                e["updated_at"] = None
        return entries
    
    for e in entries:
        if e.get("status") != "active":
            continue
        if e.get("id") == new_entry.get("id"):
            continue
        if _entries_conflict(e, new_entry):
            e["status"] = "superseded"
            e["superseded_by"] = new_entry.get("id")
            e["updated_at"] = None
    return entries



