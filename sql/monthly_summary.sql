-- =============================================================================
-- FinTrust: running balance per account
-- Brief slot: Week 14 Day 4 (SQL Analytics and Reporting Layer)
--
--     psql -f sql/monthly_summary.sql
--
-- One row per transaction, with the account's balance after that transaction.
--
-- The window function, piece by piece:
--
--   SUM(signed_amount)          add up the amounts ...
--   OVER (                      ... not for the whole table, but over a "window" of rows:
--     PARTITION BY account_id     each account separately (the sum restarts per account)
--     ORDER BY tx_date            in date order
--     ROWS UNBOUNDED PRECEDING    from the account's first row up to the current row
--   )
--
-- So each row's running_balance = all of that account's earlier amounts + its own.
-- Unlike GROUP BY, a window function keeps every row; it adds a column.
--
-- amount is always stored positive, so it is first given a sign:
--   CREDIT (money in) counts as +amount, DEBIT (money out) as -amount.
--
-- transaction_id is added to the ORDER BY only as a tie-breaker: if two
-- transactions on one account share the exact same timestamp, their order (and
-- so the running balance shown on each) would otherwise be arbitrary.
--
-- tx_month is the calendar month in South African time, so the report Lambda
-- (fintrust/report.py, which runs this same file) can select one month.
--
-- A hand-checked example is in docs/sql-analytics.md.
-- Read-only.
-- =============================================================================

SELECT
    account_id,
    transaction_id,
    tx_date,
    to_char(tx_date AT TIME ZONE 'Africa/Johannesburg', 'YYYY-MM') AS tx_month,
    tx_type,
    amount,
    CASE WHEN tx_type = 'CREDIT' THEN amount ELSE -amount END AS signed_amount,
    SUM(CASE WHEN tx_type = 'CREDIT' THEN amount ELSE -amount END)
        OVER (PARTITION BY account_id ORDER BY tx_date, transaction_id ROWS UNBOUNDED PRECEDING)
        AS running_balance
FROM transactions
ORDER BY account_id, tx_date, transaction_id;
