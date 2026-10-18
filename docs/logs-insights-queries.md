# CloudWatch Logs Insights Queries

**Brief slot:** Week 13 Day 4 (structured logging) and Week 14 Day 2 (Logs Insights queries for failed ingestion).

Every Lambda writes one JSON object per log line, so CloudWatch Logs Insights can filter and summarise on any field.

Log lines never contain customer names, contact details or free-text descriptions. Account ids are masked.

## How to run a saved query

The twelve queries below are saved in the account by `infrastructure/monitoring/deploy_monitoring.sh`.

1. Open the CloudWatch console and choose **Logs Insights** in the left menu.
2. On the right, open **Saved and sample queries** and expand the **FinTrust** folder.
3. Click a query. The log groups and the query text are filled in for you.
4. Choose a time range at the top right, then **Run query**.

The definitions are kept in `infrastructure/monitoring/queries.json`; a test checks that this page matches that file.

## Saved queries

### 1 Failed ingestion (files rejected and errors)

The brief's required query. Shows every ERROR line from the ingest Lambda: files rejected outright (wrong header, not UTF-8, empty) and any AWS failure.

Log groups: `/aws/lambda/fintrust-capstone-ingest`

```
fields @timestamp, level, message, source_key, reason, error
| filter level = "ERROR"
| sort @timestamp desc
| limit 50
```

### 2 Files processed with row counts

One line per file with how many rows were read, valid, quarantined and queued.

Log groups: `/aws/lambda/fintrust-capstone-ingest`

```
fields @timestamp, source_key, status, rows_read, rows_valid, rows_quarantined, rows_queued
| filter message = "file_processed"
| sort @timestamp desc
| limit 50
```

### 3 Reconciliation (rows in = queued + quarantined)

A control total per hour. `rows_in` must equal `queued` plus `quarantined`; if it does not, rows went missing.

Log groups: `/aws/lambda/fintrust-capstone-ingest`

```
filter message = "file_processed"
| stats sum(rows_read) as rows_in, sum(rows_queued) as queued, sum(rows_quarantined) as quarantined by bin(1h)
```

### 4 Failed scoring attempts

Each failed scoring attempt, with the attempt number (1 to 3) and the reason. Three lines for one transaction means it is now in the dead-letter queue.

Log groups: `/aws/lambda/fintrust-capstone-score`

```
fields @timestamp, transaction_id, attempt, error_type, error
| filter message = "scoring_failed"
| sort @timestamp desc
| limit 50
```

### 5 High risk transactions and the rules that fired

Every transaction scored HIGH. Add `rules.0, rules.1` to the `fields` line to see which rules fired.

Log groups: `/aws/lambda/fintrust-capstone-score`

```
fields @timestamp, transaction_id, risk_score, risk_level
| filter message = "transaction_scored" and risk_level = "HIGH"
| sort @timestamp desc
| limit 50
```

### 6 Alert emails sent and failed

The alert Lambda's outcomes: sent, failed, or skipped because an email had already been sent.

Log groups: `/aws/lambda/fintrust-capstone-alert`

```
fields @timestamp, message, transaction_id, ses_message_id, error
| filter message in ["alert_sent", "alert_send_failed", "alert_already_sent"]
| sort @timestamp desc
| limit 50
```

### 7 API errors by route

Counts of error responses per route and status code. Many 400s on one route suggests a confusing parameter; any 500 needs attention.

Log groups: `/aws/lambda/fintrust-capstone-query`

```
filter level = "ERROR" or status >= 400
| stats count(*) as responses by resource, status
| sort responses desc
```

### 8 One transaction across the whole pipeline

Follows one transaction through every function by its id. Replace the placeholder text with a real transaction id.

Log groups: `/aws/lambda/fintrust-capstone-ingest`, `/aws/lambda/fintrust-capstone-score`, `/aws/lambda/fintrust-capstone-alert`, `/aws/lambda/fintrust-capstone-query`, `/aws/lambda/fintrust-capstone-report`

```
fields @timestamp, service, level, message, attempt, risk_score, error
| filter transaction_id = "PASTE-TRANSACTION-ID-HERE"
| sort @timestamp asc
```

### 9 All errors in every function

Everything that went wrong anywhere, newest first. This is the first query to run when an alarm fires.

Log groups: `/aws/lambda/fintrust-capstone-ingest`, `/aws/lambda/fintrust-capstone-score`, `/aws/lambda/fintrust-capstone-alert`, `/aws/lambda/fintrust-capstone-query`, `/aws/lambda/fintrust-capstone-report`

```
fields @timestamp, service, message, transaction_id, source_key, error
| filter level = "ERROR"
| sort @timestamp desc
| limit 100
```

### 10 Monthly reports written and failed

One line per report run: the month, how many transactions and accounts it covered, and where the file was written. A `report_failed` line carries the reason.

Log groups: `/aws/lambda/fintrust-capstone-report`

```
fields @timestamp, level, message, month, transactions, accounts, high_risk_accounts, json_key, error
| filter message in ["report_written", "report_failed", "control_totals_disagree"]
| sort @timestamp desc
| limit 50
```

### 11 Who changed what (resolves, flags, uploads)

Week 15 Day 2. Every change to the record, with the signed-in person who made it. The username comes from the person's Cognito sign-in, never from the request, so this is the audit trail a reviewer or the information officer asks for (POPIA accountability).

Log groups: `/aws/lambda/fintrust-capstone-query`

```
fields @timestamp, actor, message, alert_id, account, object_key
| filter message in ["alert_resolved", "account_flagged", "upload_accepted"]
| sort @timestamp desc
| limit 100
```

### 12 Refused requests by person (sign-in, session, role)

Week 15 Day 2. Requests the API refused: 401 is no sign-in or a session that has ended, 403 is a role that does not allow the action (an analyst trying to resolve an alert). A sudden rise for one person is worth a look.

Log groups: `/aws/lambda/fintrust-capstone-query`

```
fields @timestamp, actor, method, resource, status
| filter message = "request_completed" and status in [401, 403]
| stats count(*) as refused by actor, resource, status
| sort refused desc
```

## More queries for the ingest Lambda

Log group `/aws/lambda/fintrust-capstone-ingest`. These are not saved in the account; paste them in.

**How many rows were quarantined per file**

```
fields @timestamp, source_key, count, quarantine_key
| filter message = "rows_quarantined"
| sort @timestamp desc
```

**Everything that happened in one invocation**

```
fields @timestamp, level, message, source_key
| filter request_id = "PASTE-REQUEST-ID-HERE"
| sort @timestamp asc
```

**Files containing columns the system does not need (POPIA minimality)**

```
fields @timestamp, source_key, columns
| filter message = "columns_ignored"
```

## The metric lines

Each function also prints one line per run with `message = "metrics"`. CloudWatch turns these into the custom metrics in the `FinTrust/ETL` namespace (see `fintrust/metrics.py`). They are ordinary log lines too:

```
fields @timestamp, RowsRead, RowsQueued, RowsQuarantined, FilesRejected
| filter message = "metrics"
| sort @timestamp desc
```
