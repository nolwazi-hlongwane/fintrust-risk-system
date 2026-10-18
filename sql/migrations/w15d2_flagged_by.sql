-- =============================================================================
-- FinTrust: upgrade a deployed database for Week 15 Day 2 (secure sign-in)
--
-- schema.sql builds a NEW database. A database that already holds data is
-- changed with this file instead, so nothing in it is lost:
--
--     python scripts/run_sql.py sql/migrations/w15d2_flagged_by.sql sql/functions.sql sql/db_users.sql
--
-- Adds risk_events.flagged_by: the signed-in officer who flagged an account.
-- Safe to run more than once. All or nothing (one transaction).
-- =============================================================================

BEGIN;

ALTER TABLE risk_events ADD COLUMN IF NOT EXISTS flagged_by VARCHAR(60);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_risk_events_flagged_by') THEN
        ALTER TABLE risk_events ADD CONSTRAINT ck_risk_events_flagged_by
            CHECK (flagged_by IS NULL OR event_type = 'MANUAL_FLAG');
    END IF;
END
$$;

COMMIT;
