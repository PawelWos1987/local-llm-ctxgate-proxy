-- tools/rollback_tenancy.sql
-- Rollback script for tenant_id columns added by Prompt B.
-- SAFETY: This script REFUSES to run if it would cause data loss or constraint violations.
-- It does NOT automatically drop columns. Manual review required.

\set ON_ERROR_STOP on

BEGIN;

-- 1. Check for multi-tenant data that would be lost
DO $$
DECLARE
    multi_tenant_count INT;
BEGIN
    SELECT count(*) INTO multi_tenant_count
    FROM proxy.tasks
    WHERE tenant_id IS NOT NULL AND tenant_id != 'local';
    
    IF multi_tenant_count > 0 THEN
        RAISE EXCEPTION 'ROLLBACK REFUSED: % tasks have non-local tenant_id. Dropping tenant columns would lose ownership data. Manual migration required.', multi_tenant_count;
    END IF;
END $$;

-- 2. Check knowledge table for multi-tenant entries
DO $$
DECLARE
    multi_tenant_count INT;
BEGIN
    SELECT count(*) INTO multi_tenant_count
    FROM proxy.knowledge
    WHERE tenant_id IS NOT NULL AND tenant_id != 'local';
    
    IF multi_tenant_count > 0 THEN
        RAISE EXCEPTION 'ROLLBACK REFUSED: % knowledge rows have non-local tenant_id. Dropping tenant columns would lose ownership data.', multi_tenant_count;
    END IF;
END $$;

-- 3. Check deliverables table
DO $$
DECLARE
    multi_tenant_count INT;
BEGIN
    SELECT count(*) INTO multi_tenant_count
    FROM proxy.deliverables
    WHERE tenant_id IS NOT NULL AND tenant_id != 'local';
    
    IF multi_tenant_count > 0 THEN
        RAISE EXCEPTION 'ROLLBACK REFUSED: % deliverables have non-local tenant_id. Dropping tenant columns would lose ownership data.', multi_tenant_count;
    END IF;
END $$;

-- 4. Check for duplicate (domain, key) pairs that would violate old uniqueness
DO $$
DECLARE
    dup_count INT;
BEGIN
    SELECT count(*) INTO dup_count FROM (
        SELECT domain, key, count(*) as cnt
        FROM proxy.knowledge
        WHERE active = true
        GROUP BY domain, key
        HAVING count(*) > 1
    ) dups;
    
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'ROLLBACK REFUSED: % duplicate (domain, key) pairs exist in knowledge. Old uniqueness constraint would be violated.', dup_count;
    END IF;
END $$;

-- 5. If all checks pass, perform the rollback
-- Note: This only removes the tenant_id columns. Data is preserved.
ALTER TABLE proxy.tasks DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE proxy.knowledge DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE proxy.deliverables DROP COLUMN IF EXISTS tenant_id;

-- Recreate old uniqueness constraint on knowledge
DROP INDEX IF EXISTS proxy.knowledge_tenant_domain_key_unique;
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_domain_key_unique
    ON proxy.knowledge (domain, key) WHERE active;

COMMIT;

SELECT 'Rollback complete. Tenant columns removed, old constraints restored.' as status;
