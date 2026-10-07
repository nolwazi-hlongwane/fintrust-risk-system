-- =============================================================================
-- FinTrust: views for the compliance team
-- Brief slot: Week 14 Day 4 (SQL Analytics and Reporting Layer)
--
-- Run after schema.sql and functions.sql, before db_users.sql:
--     psql -f sql/views.sql
-- Safe to run more than once (CREATE OR REPLACE).
--
-- A view is a saved query that behaves like a table. The compliance team can
-- write   SELECT * FROM v_risk_dashboard WHERE open_alerts > 0;
-- without knowing which tables exist or how they join.
--
-- Views are also a privacy control (POPIA minimality). None of these views
-- exposes a customer's name, email address or phone number, so a user who is
-- granted the views (and not the customers table) cannot see them.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- v_risk_dashboard: one row per account, everything needed to judge its risk.
--   Each kind of fact is counted in its own subquery and then joined. Joining
--   transactions, alerts and risk_events directly to each other would repeat
--   rows and inflate the counts.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_risk_dashboard AS
SELECT
    a.account_id,
    a.customer_id,
    a.account_type,
    a.status                                   AS account_status,
    COALESCE(t.transaction_count, 0)           AS transaction_count,
    COALESCE(t.total_credits, 0)               AS total_credits,
    COALESCE(t.total_debits, 0)                AS total_debits,
    COALESCE(t.high_risk_count, 0)             AS high_risk_count,
    COALESCE(t.medium_risk_count, 0)           AS medium_risk_count,
    COALESCE(t.highest_risk_score, 0)          AS highest_risk_score,
    COALESCE(t.average_risk_score, 0)          AS average_risk_score,
    COALESCE(t.total_risk_points, 0)           AS total_risk_points,
    t.last_transaction_at,
    t.last_high_risk_at,
    COALESCE(al.open_alerts, 0)                AS open_alerts,
    COALESCE(al.resolved_alerts, 0)            AS resolved_alerts,
    COALESCE(f.manual_flags, 0)                AS manual_flags
FROM accounts a
LEFT JOIN (
    SELECT account_id,
           COUNT(*)                                             AS transaction_count,
           SUM(amount) FILTER (WHERE tx_type = 'CREDIT')        AS total_credits,
           SUM(amount) FILTER (WHERE tx_type = 'DEBIT')         AS total_debits,
           COUNT(*) FILTER (WHERE risk_level = 'HIGH')          AS high_risk_count,
           COUNT(*) FILTER (WHERE risk_level = 'MEDIUM')        AS medium_risk_count,
           MAX(risk_score)                                      AS highest_risk_score,
           ROUND(AVG(risk_score), 1)                            AS average_risk_score,
           SUM(risk_score)                                      AS total_risk_points,
           MAX(tx_date)                                         AS last_transaction_at,
           MAX(tx_date) FILTER (WHERE risk_level = 'HIGH')      AS last_high_risk_at
    FROM transactions
    GROUP BY account_id
) t ON t.account_id = a.account_id
LEFT JOIN (
    SELECT tx.account_id,
           COUNT(*) FILTER (WHERE al.status = 'OPEN')           AS open_alerts,
           COUNT(*) FILTER (WHERE al.status = 'RESOLVED')       AS resolved_alerts
    FROM alerts al
    JOIN transactions tx ON tx.transaction_id = al.transaction_id
    GROUP BY tx.account_id
) al ON al.account_id = a.account_id
LEFT JOIN (
    SELECT account_id, COUNT(*) AS manual_flags
    FROM risk_events
    WHERE event_type = 'MANUAL_FLAG'
    GROUP BY account_id
) f ON f.account_id = a.account_id;

COMMENT ON VIEW v_risk_dashboard IS
    'One row per account: activity, risk counts, alerts and manual flags. No customer contact details.';

-- -----------------------------------------------------------------------------
-- v_open_alerts: the compliance work queue, oldest first.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_open_alerts AS
SELECT
    al.alert_id,
    al.created_at                              AS alert_raised_at,
    ROUND((EXTRACT(EPOCH FROM (now() - al.created_at)) / 3600)::numeric, 1) AS hours_open,
    t.transaction_id,
    t.account_id,
    a.status                                   AS account_status,
    t.tx_date,
    t.amount,
    t.currency,
    t.tx_type,
    t.channel,
    t.country_code,
    t.risk_score,
    (SELECT string_agg(e.event_type, ', ' ORDER BY e.event_type)
     FROM risk_events e
     WHERE e.transaction_id = t.transaction_id) AS rules_fired
FROM alerts al
JOIN transactions t ON t.transaction_id = al.transaction_id
JOIN accounts a     ON a.account_id = t.account_id
WHERE al.status = 'OPEN'
ORDER BY al.created_at;

COMMENT ON VIEW v_open_alerts IS
    'Open alerts with their transaction and the rules that fired, oldest first.';
