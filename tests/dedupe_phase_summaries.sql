-- ============================================================================
-- DB cleanup for proxy.phase_summaries  --  PRINT/REVIEW ONLY, DO NOT RUN on prod
-- ----------------------------------------------------------------------------
-- Context (diagnosed 2026-10-05): the old _summarize_trimmed_messages had BUG 1
-- (stored_any never set True) -> the SAME slice was re-summarized on every turn,
-- so task f8c9239d accumulated 608 phase rows.
--
-- KEY FINDING: for EVERY task, count(DISTINCT summary) == count(*). The 4B model
-- is non-deterministic and emits a UNIQUE summary on each re-run, so there are
-- NO exact-duplicate rows to remove. The bloat is SEMANTIC (many near-identical
-- rows), not exact duplicates. An exact-match dedupe therefore removes 0 rows.
--
-- The real fix is the code fix (BUG 1/2) which is already in app.py and has
-- stopped the growth (all session_windows rows are caught up:
--   summarized_through == dropped_total for every session).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. DIAGNOSTIC: run this first (read-only, safe).
-- ---------------------------------------------------------------------------
SELECT task_id,
       count(*)                  AS total_rows,
       count(DISTINCT summary)   AS distinct_summaries,
       (count(*) - count(DISTINCT summary)) AS exact_dup_rows
FROM proxy.phase_summaries
GROUP BY task_id
ORDER BY total_rows DESC;

-- ---------------------------------------------------------------------------
-- 2. EXACT-DEDUPE (keeps the EARLIEST row of each identical summary, per task).
--    In a transaction; inspects the delete count BEFORE committing.
--    NOTE: expected to delete 0 rows given the key finding above.
-- ---------------------------------------------------------------------------
BEGIN;
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY task_id, summary
               ORDER BY created_at ASC, phase_number ASC
           ) AS rn
    FROM proxy.phase_summaries
)
DELETE FROM proxy.phase_summaries p
USING ranked r
WHERE p.id = r.id AND r.rn > 1;
-- Inspect:  SELECT count(*) AS deleted;   -- expect 0
-- ROLLBACK;            -- undo
-- COMMIT;              -- keep
ROLLBACK;

-- ---------------------------------------------------------------------------
-- 3. SEMANTIC DEDUPE (the bloat that actually exists).
--    Keep the earliest row per (task, session, phase_number); drop the rest.
--    The old loop kept bumping phase_number and re-inserting, so collapsing per
--    (task, session, phase) removes the redundant re-summarized copies while
--    keeping a single representative per phase. REVIEW the kept set first.
-- ---------------------------------------------------------------------------
-- Pre-flight: how many would survive?
SELECT task_id, session_key,
       count(*) AS rows,
       min(created_at) AS earliest
FROM proxy.phase_summaries
WHERE task_id = 'f8c9239d-b8b7-462f-a0f9-ed7027dbbf36'
GROUP BY task_id, session_key, phase_number
ORDER BY phase_number;

-- (apply only after reviewing the kept set; in a transaction)
-- BEGIN;
-- WITH ranked AS (
--     SELECT id,
--            row_number() OVER (
--                PARTITION BY task_id, session_key, phase_number
--                ORDER BY created_at ASC
--            ) AS rn
--     FROM proxy.phase_summaries
-- )
-- DELETE FROM proxy.phase_summaries p
-- USING ranked r
-- WHERE p.id = r.id AND r.rn > 1
--   AND p.task_id = 'f8c9239d-b8b7-462f-a0f9-ed7027dbbf36';
-- ROLLBACK;   -- or COMMIT;

-- ---------------------------------------------------------------------------
-- 4. FORCED ROOT-SUMMARY REBUILD (per task).
--    Regenerate ONE clean root summary from the (now-deduped) phase history and
--    store it as the authoritative session summary, matching what
--    _summarize_trimmed_messages does at the root step.
--    The 4B model call is done OUTSIDE SQL; this only persists its result.
-- ---------------------------------------------------------------------------
-- Step 1: build the phase history to feed the 4B root distiller:
SELECT string_agg(
           'Phase ' || phase_number || '' || '' || summary || E'\n',
           E'\n'
       ) AS phase_history
FROM proxy.phase_summaries
WHERE task_id = 'f8c9239d-b8b7-462f-a0f9-ed7027dbbf36'
ORDER BY phase_number;
-- Step 2: pass phase_history to the 4B model with the MISTRAL_SYSTEM_PROMPT
--         (json_mode) to get a <=400-word state summary.
-- Step 3: persist it (cap 6000) as the new authoritative root:
INSERT INTO proxy.session_summaries
    (task_id, session_key, summary, trimmed_msg_count, trimmed_tokens)
VALUES
    ('f8c9239d-b8b7-462f-a0f9-ed7027dbbf36',
     (SELECT session_key FROM proxy.session_windows
      WHERE 1=1 LIMIT 1),
     '<PASTE 4B ROOT SUMMARY HERE, <=6000 chars>',
     0, 0)
ON CONFLICT DO NOTHING;
-- (confirm the table has no PK/unique for ON CONFLICT; adjust to a plain INSERT
--  or a delete-then-insert if session_summaries has no conflict target.)

-- ============================================================================
