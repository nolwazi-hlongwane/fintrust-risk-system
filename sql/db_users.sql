-- =============================================================================
-- FinTrust: database users, one per Lambda that connects to PostgreSQL
-- Brief slot: Week 13 Day 2 (Data Layer)
--
-- Run after schema.sql:   psql -f sql/db_users.sql
-- Safe to run more than once.
--
-- Least privilege inside the database, as a second layer behind IAM:
--   fintrust_score   writes the ledger          (score Lambda)
--   fintrust_query   reads, and resolves alerts (query Lambda)
--   fintrust_report  reads only                 (report Lambda)
--
-- None of these users has a password in AWS. Each is granted the rds_iam role,
-- which tells RDS to accept only a short-lived token signed by the Lambda's
-- IAM role. On a local PostgreSQL (tests) rds_iam does not exist, so that
-- grant is skipped.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    user_name TEXT;
BEGIN
    FOREACH user_name IN ARRAY ARRAY['fintrust_score', 'fintrust_query', 'fintrust_report']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = user_name) THEN
            EXECUTE format('CREATE ROLE %I LOGIN', user_name);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
            EXECUTE format('GRANT rds_iam TO %I', user_name);
        END IF;
    END LOOP;
END
$$;

-- Start from nothing, then grant exactly what each user needs.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fintrust_score, fintrust_query, fintrust_report;
GRANT USAGE ON SCHEMA public TO fintrust_score, fintrust_query, fintrust_report;

-- score Lambda: checks the account exists, counts recent transactions for the
-- velocity rule, then inserts the transaction, its rule events and any alert.
-- It cannot update or delete anything: the ledger is append-only.
GRANT SELECT ON accounts, transactions TO fintrust_score;
GRANT INSERT ON transactions, risk_events, alerts TO fintrust_score;

-- query Lambda: reads everything for the API, and may change only the four
-- resolution columns of an alert.
GRANT SELECT ON customers, accounts, transactions, risk_events, alerts TO fintrust_query;
GRANT UPDATE (status, resolved_at, resolved_by, resolution_note) ON alerts TO fintrust_query;

-- report Lambda: read only.
GRANT SELECT ON customers, accounts, transactions, risk_events, alerts TO fintrust_report;

COMMIT;
