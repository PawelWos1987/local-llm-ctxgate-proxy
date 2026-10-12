"""Knowledge storage."""



async def store_knowledge(items: list, source_session: str, source_key: str) -> int:
    """Upsert knowledge items into the global knowledge table."""
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    if not pool or not items:
        return 0
    stored = 0
    for item in items:
        try:
            await pool.execute(
                "INSERT INTO proxy.knowledge (domain, key, value, importance, source_session, source_key, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, now()) "
                "ON CONFLICT (domain, key) WHERE active = true "
                "DO UPDATE SET value = EXCLUDED.value, importance = GREATEST(EXCLUDED.importance, proxy.knowledge.importance), "
                "source_session = EXCLUDED.source_session, source_key = EXCLUDED.source_key, updated_at = now()",
                item["domain"], item["key"], item["value"], item.get("importance", 5), source_session, source_key
            )
            stored += 1
        except Exception as e:
            log.warning("Knowledge store failed: %s", e)
    return stored



