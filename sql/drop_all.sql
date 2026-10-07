-- DEVELOPMENT ONLY: removes every FinTrust table so schema.sql can be run again.
-- This deletes all ledger data. Never run it against a database you want to keep.

BEGIN;
DROP FUNCTION IF EXISTS flag_account(VARCHAR, VARCHAR);
DROP TABLE IF EXISTS alerts, risk_events, transactions, accounts, customers CASCADE;
COMMIT;
