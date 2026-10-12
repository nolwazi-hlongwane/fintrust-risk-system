-- DEVELOPMENT ONLY: removes every FinTrust table, view and function so schema.sql can be run again.
-- This deletes all ledger data. Never run it against a database you want to keep.

BEGIN;
DROP VIEW IF EXISTS v_open_alerts, v_risk_dashboard;
DROP FUNCTION IF EXISTS flag_account(VARCHAR, VARCHAR);
DROP TABLE IF EXISTS alerts, risk_events, transactions, accounts, customers CASCADE;
COMMIT;
