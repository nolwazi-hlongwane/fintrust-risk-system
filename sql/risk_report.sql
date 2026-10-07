-- =============================================================================
-- FinTrust: high-risk account report
-- Brief slot: Week 14 Day 4 (SQL Analytics and Reporting Layer)
--
--     psql -f sql/risk_report.sql
--
-- Question 1 (the brief): which accounts had MORE THAN 3 high-risk transactions
--                         in ANY rolling 30-day window?
-- Question 2 (fraud ranking): how does each of those accounts rank against all
--                         accounts, as a percentile (PERCENT_RANK)?
--
-- Read-only. This same file is run by the report Lambda (fintrust/report.py),
-- so the monthly report and this query can never disagree.
--
-- How it works, one CTE (named step) at a time:
--
--   high_risk        only the HIGH risk transactions.
--
--   rolling          for EACH high-risk transaction, count the account's
--                    high-risk transactions in the 30 days up to and including
--                    it. The window "slides" along the account's history:
--                        RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW
--                    RANGE works on the VALUE of tx_date (a period of time),
--                    where ROWS would count a fixed number of rows.
--
--   breaches         keep accounts whose count went above 3 at any point, with
--                    the highest count reached and when it first happened.
--
--   account_totals   one row per account with any activity: its total risk
--                    points (the sum of its transactions' risk scores).
--
--   ranked           PERCENT_RANK() places every account between 0 and 1 by
--                    total risk points: 0 = lowest in the bank, 1 = highest.
--                    It is calculated over ALL accounts BEFORE filtering, so
--                    0.91 means "riskier than 91% of all other accounts".
-- =============================================================================

WITH high_risk AS (
    SELECT account_id, transaction_id, tx_date, amount
    FROM transactions
    WHERE risk_level = 'HIGH'
),
rolling AS (
    SELECT account_id,
           tx_date,
           COUNT(*) OVER (
               PARTITION BY account_id
               ORDER BY tx_date
               RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW
           ) AS high_risk_in_30_days
    FROM high_risk
),
breaches AS (
    SELECT account_id,
           MAX(high_risk_in_30_days)                                AS peak_high_risk_in_30_days,
           MIN(tx_date) FILTER (WHERE high_risk_in_30_days > 3)     AS first_breach_at,
           MAX(tx_date) FILTER (WHERE high_risk_in_30_days > 3)     AS latest_breach_at
    FROM rolling
    GROUP BY account_id
    HAVING MAX(high_risk_in_30_days) > 3
),
account_totals AS (
    SELECT account_id,
           COUNT(*)                                         AS transaction_count,
           COUNT(*) FILTER (WHERE risk_level = 'HIGH')      AS high_risk_count,
           SUM(amount) FILTER (WHERE risk_level = 'HIGH')   AS high_risk_value,
           SUM(risk_score)                                  AS total_risk_points
    FROM transactions
    GROUP BY account_id
),
ranked AS (
    SELECT account_totals.*,
           ROUND(PERCENT_RANK() OVER (ORDER BY total_risk_points)::numeric, 4) AS fraud_percentile,
           RANK() OVER (ORDER BY total_risk_points DESC)                       AS fraud_rank
    FROM account_totals
)
SELECT
    b.account_id,
    a.customer_id,
    a.account_type,
    a.status                         AS account_status,
    b.peak_high_risk_in_30_days,
    b.first_breach_at,
    b.latest_breach_at,
    r.high_risk_count,
    r.high_risk_value,
    r.transaction_count,
    r.total_risk_points,
    r.fraud_rank,
    r.fraud_percentile
FROM breaches b
JOIN ranked r   ON r.account_id = b.account_id
JOIN accounts a ON a.account_id = b.account_id
ORDER BY r.fraud_rank, b.account_id;
