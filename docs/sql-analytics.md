# SQL Analytics and the Monthly Report

**Brief slot:** Week 14 Day 4: SQL Analytics and Reporting Layer
**Rubric:** C2 SQL and Data Modelling

This page explains the three analytics files in `sql/`, shows the running balance checked by hand, and describes the monthly report that is built from them.

| File | What it answers | Technique |
|---|---|---|
| `sql/monthly_summary.sql` | What was each account's balance after every transaction? | Window function: `SUM() OVER (PARTITION BY ... ORDER BY ... ROWS UNBOUNDED PRECEDING)` |
| `sql/risk_report.sql` | Which accounts had more than 3 high-risk transactions in any rolling 30 days, and how do they rank? | CTEs, a sliding `RANGE` window, `PERCENT_RANK()` |
| `sql/views.sql` | What does the compliance team need to see, without knowing the tables? | Views `v_risk_dashboard` and `v_open_alerts` |

To run any of them (tunnel open): `python scripts/run_sql.py sql/risk_report.sql`

## 1. Running balance: `monthly_summary.sql`

```sql
SUM(CASE WHEN tx_type = 'CREDIT' THEN amount ELSE -amount END)
    OVER (PARTITION BY account_id ORDER BY tx_date, transaction_id ROWS UNBOUNDED PRECEDING)
    AS running_balance
```

Read it from the inside out:

1. **`CASE ...`** Amounts are stored as positive numbers. A CREDIT (money in) counts as plus, a DEBIT (money out) as minus.
2. **`PARTITION BY account_id`** Each account is handled separately. The sum starts again from zero for every account.
3. **`ORDER BY tx_date`** Within an account, rows are taken in date order. `transaction_id` is only a tie-breaker for two transactions with the same timestamp.
4. **`ROWS UNBOUNDED PRECEDING`** For each row, add up everything from the account's first row to this row.

A window function differs from `GROUP BY`: `GROUP BY` collapses many rows into one, while a window function keeps every row and adds a column to it.

### Hand verification

Account `ACC-1001`, its first eight transactions in the seed data. The "worked by hand" column was calculated with a calculator from the amounts alone; the last column is what the query returned.

| # | Date (SAST) | Type | Amount | Worked by hand | Query result |
|---|---|---|---|---|---|
| 1 | 2026-07-01 08:20 | CREDIT | 7 903.00 | 0.00 + 7 903.00 = **7 903.00** | 7903.00 |
| 2 | 2026-07-07 13:16 | DEBIT | 1 847.48 | 7 903.00 − 1 847.48 = **6 055.52** | 6055.52 |
| 3 | 2026-07-08 07:39 | DEBIT | 2 512.60 | 6 055.52 − 2 512.60 = **3 542.92** | 3542.92 |
| 4 | 2026-07-11 08:35 | DEBIT | 1 167.79 | 3 542.92 − 1 167.79 = **2 375.13** | 2375.13 |
| 5 | 2026-07-13 18:32 | DEBIT | 1 140.30 | 2 375.13 − 1 140.30 = **1 234.83** | 1234.83 |
| 6 | 2026-07-18 13:15 | DEBIT | 1 249.00 | 1 234.83 − 1 249.00 = **−14.17** | -14.17 |
| 7 | 2026-07-18 20:46 | DEBIT | 470.23 | −14.17 − 470.23 = **−484.40** | -484.40 |
| 8 | 2026-07-25 06:16 | CREDIT | 28 500.00 | −484.40 + 28 500.00 = **28 015.60** | 28015.60 |

Every row agrees. Rows 6 and 7 show the account briefly overdrawn before the salary arrives in row 8, which is the kind of detail a running balance exists to reveal.

The account's last two July transactions (debits of 466.49 and 382.66) bring the month-end balance to **27 166.45**, which is the closing balance the monthly report shows for `ACC-1001` in July.

Three automated tests keep this true (`tests/integration/test_sql_analytics.py`): one asserts these eight values exactly, one recomputes the balance of all 275 seed transactions in plain Python and compares every row, and one checks that the sum restarts for each account.

## 2. High-risk accounts: `risk_report.sql`

The brief asks for accounts with **more than 3 high-risk transactions in any rolling 30-day window**. The query is built from five named steps (CTEs, written with `WITH`), each small enough to read on its own:

| Step | What it produces |
|---|---|
| `high_risk` | Only the transactions scored HIGH |
| `rolling` | For each of those, how many HIGH transactions the same account had in the 30 days up to and including it |
| `breaches` | Accounts where that count went above 3 at any point, with the peak and the dates |
| `account_totals` | For every account: its transaction count and total risk points |
| `ranked` | Every account's `PERCENT_RANK()` and rank by total risk points |

The sliding window is this line:

```sql
COUNT(*) OVER (PARTITION BY account_id ORDER BY tx_date
               RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW)
```

`RANGE` works on the value of `tx_date` (a span of time), where `ROWS` would count a fixed number of rows. "Rolling" means the 30 days are measured back from every transaction, not from the start of a calendar month, so a burst that straddles two months is still caught.

**`PERCENT_RANK()`** places each account between 0 and 1: `(rank − 1) ÷ (number of accounts − 1)`. It is calculated across all accounts before the list is filtered, so a value of 0.9091 means "riskier than about 91% of all accounts".

### Result on the seed data

| Account | Peak HIGH in 30 days | First breach | HIGH transactions | Total risk points | Rank | Percentile |
|---|---|---|---|---|---|---|
| `ACC-1007` | 5 | 2026-09-09 | 5 | 550 | 2 | 0.9091 |

Two details worth knowing for questions:

- **`ACC-1005` is not listed**, although it has 4 HIGH transactions and more total risk points (580, rank 1). Its four are spread over three months, never more than 2 inside 30 days. The rule is about a burst, not a lifetime total.
- **Percentile check by hand:** 12 accounts have transactions; 10 have fewer risk points than `ACC-1007`. 10 ÷ (12 − 1) = 0.9091.

Tests compare the query with a brute-force check of every window in Python, and test the boundary: exactly 3 is not reported, a 4th exactly 30 days after the 1st is, and a 4th 31 days after is not.

## 3. Views: `views.sql`

| View | One row per | Use |
|---|---|---|
| `v_risk_dashboard` | Account | Activity, risk counts, total risk points, open and resolved alerts, manual flags |
| `v_open_alerts` | Open alert | The compliance work queue, oldest first, with the transaction and the rules that fired |

A compliance analyst can write `SELECT * FROM v_risk_dashboard WHERE open_alerts > 0 ORDER BY total_risk_points DESC;` without knowing that five tables exist.

Two design points:

- **No inflated counts.** An account has many transactions, several alerts and possibly manual flags. Joining those tables directly would multiply the rows. The view counts each in its own subquery and then joins the three results, one row per account each.
- **A privacy control.** Neither view contains a name, email address or phone number. A database user can be given the views without the `customers` table. The report Lambda's user (`fintrust_report`) has exactly that: it cannot read `customers` at all.

## 4. The monthly report

The report Lambda (`fintrust/report.py`) runs at 06:00 South African time on the 1st of each month (EventBridge rule `fintrust-capstone-monthly-report`, `cron(0 4 1 * ? *)` in UTC) and reports on the month just ended. It does not contain its own copy of the analytics: it **runs the two SQL files above** and reads `v_open_alerts`, so the report and the SQL artefacts cannot disagree.

It writes two files to the reports bucket:

| File | Contents |
|---|---|
| `monthly/2026-09/fintrust-report-2026-09.json` | The full report: month totals, rules fired, alert counts, one entry per account, the high-risk accounts, the open alerts |
| `monthly/2026-09/fintrust-report-2026-09.csv` | One row per account for the month: opening balance, credits, debits, closing balance, HIGH count, and whether it breached the 30-day rule |

Built-in checks:

- **Control totals.** The per-account rows must add up to the month's totals (count, credits, debits, HIGH count). The result is written into the report as `control_totals_agree` and logged as an error if false.
- **Balances chain.** Each account's opening balance is its closing balance minus the month's net movement, so one month's closing balance is the next month's opening balance.

Scope of each section: the month totals and the per-account rows cover the calendar month in South African time. The high-risk accounts and open alerts describe the whole ledger at the moment the report is generated, because a rolling 30-day window does not stop at a month boundary.

The report identifies accounts by id only. It contains no names or contact details, and the bucket is encrypted with the project KMS key and follows the 7-year retention lifecycle.

To produce a report for any month by hand:

```bash
aws lambda invoke --function-name fintrust-capstone-report --cli-binary-format raw-in-base64-out \
    --payload '{"month": "2026-09"}' /dev/stdout
aws s3 cp s3://fintrust-capstone-reports-<account id>/monthly/2026-09/fintrust-report-2026-09.csv -
```
