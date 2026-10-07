# Monitoring and Observability

**Brief slot:** Week 14 Day 2: CloudWatch Monitoring and Observability
**Deployed by:** `infrastructure/monitoring/deploy_monitoring.sh`

This page answers three questions: how do we know the system is healthy, how do we find out when it is not, and what do we do then.

## The four tools and what each is for

| Tool | Question it answers | Where |
|---|---|---|
| **Metrics** | How much is happening? (numbers over time) | Namespace `FinTrust/ETL`, plus the metrics AWS publishes for Lambda, SQS and API Gateway |
| **Alarms** | Is something wrong right now? | 7 alarms, emailing through an SNS topic |
| **Logs** | What exactly happened? | One JSON line per event; saved Logs Insights queries |
| **Traces** | Where did the time go in one request? | X-Ray, on the API stage and every Lambda |

The dashboard `fintrust-capstone-operations` puts the first three on one page.

## Custom metrics: namespace `FinTrust/ETL`

All are counts with one dimension, `Environment = dev`.

| Metric | Published by | Meaning |
|---|---|---|
| `RowsRead` | ingest | Data rows read from an accepted file |
| `RowsQueued` | ingest | Valid rows sent to the scoring queue |
| `RowsQuarantined` | ingest | Rows rejected by validation |
| `FilesRejected` | ingest | Whole files rejected (wrong header, not text, empty) |
| `TransactionsScored` | score | Transactions written to the ledger |
| `HighRiskTransactions` | score | Of those, how many scored HIGH |
| `ScoringFailures` | score | Scoring attempts that failed and will be retried |
| `AlertsSent` | alert | Emails sent to compliance |
| `AlertFailures` | alert | Emails that could not be sent on this attempt |

**Control total:** `RowsRead` must equal `RowsQueued` + `RowsQuarantined`. If it does not, rows went missing between steps.

### How a metric is published

The Lambda prints one JSON log line in a format CloudWatch recognises (the Embedded Metric Format). CloudWatch Logs reads the line and creates the metric. The code is `fintrust/metrics.py`.

```json
{"_aws": {"Timestamp": 1791370000000, "CloudWatchMetrics": [{"Namespace": "FinTrust/ETL",
  "Dimensions": [["Environment"]], "Metrics": [{"Name": "RowsRead", "Unit": "Count"}]}]},
 "Environment": "dev", "message": "metrics", "RowsRead": 15}
```

Why this and not the CloudWatch API (`PutMetricData`):

1. The score Lambda is in the VPC with no NAT Gateway and cannot reach the CloudWatch API. Log lines are collected by the Lambda service and always arrive.
2. No API call means no added delay, no extra IAM permission, and nothing that can fail inside the function.
3. The metric line is also a normal log line, so Logs Insights can query it.

## Alarms

Every alarm emails the topic `fintrust-capstone-ops-alarms` when it fires and again when it recovers. "No data" is treated as healthy, because a quiet system is not a broken one.

| Alarm | Fires when | What to do |
|---|---|---|
| `scoring-error-rate` | More than 5% of scoring attempts failed in 5 minutes | Run saved query 4 (failed scoring attempts). If the database is stopped, start it; messages retry automatically |
| `high-risk-count` | 5 or more HIGH risk transactions in 5 minutes | Review open alerts with `GET /alerts?status=OPEN`. Possible fraud pattern or a bad batch |
| `transactions-dlq-depth` | A message is in the scoring dead-letter queue | Find the reason with saved query 4, fix the cause, then run `./scripts/redrive_dlq.sh` |
| `ingest-dlq-depth` | A file could not be processed after retries | Run saved query 1 (failed ingestion), then upload the file again |
| `alert-dlq-depth` | An alert email could not be sent after 3 retries | Check that the SES address is still verified. The alert itself is in the ledger |
| `lambda-errors` | Any function ended with an error | Open the error table at the bottom of the dashboard |
| `api-5xx-errors` | The API returned a server error | Check `GET /health`. Usually the database is stopped |

The first two are the alarms the brief asks for (error rate and high-risk transaction count). Thresholds can be changed when deploying, for example `HIGH_RISK_ALARM_THRESHOLD=10 ./infrastructure/monitoring/deploy_monitoring.sh`.

**Error rate formula:** `100 × ScoringFailures ÷ (ScoringFailures + TransactionsScored)`, or 0 when nothing was attempted.

**A note on dead-letter queue alarms:** SQS stops publishing metrics for a queue that has been empty and unused for several hours. When the first message arrives, the metric (and so the alarm) can take up to 15 minutes to appear.

### Why the alarm topic needed a change to the key policy

The topic is encrypted with the project's KMS key. CloudWatch is an AWS service, not an IAM role in this account, so it cannot be given permission through an IAM policy. It is allowed in the key policy instead (`infrastructure/kms/key-policy.json`), with only `kms:Decrypt` and `kms:GenerateDataKey`. Without this the alarm would change state but the email would silently never be sent.

## Logs

Every function logs JSON with `timestamp`, `level`, `service`, `message` and identifiers such as `transaction_id` and `request_id`. Nine queries are saved in the account under **FinTrust/**; see [logs-insights-queries.md](logs-insights-queries.md). Log groups keep 30 days of logs.

## X-Ray tracing

Active tracing is on for the API stage and all Lambda functions. A trace shows one request passing through API Gateway and the Lambda, with the time spent in each. To view: CloudWatch console, **Traces** (or **Trace Map**) in the left menu.

The functions use Lambda's built-in tracing only. The X-Ray SDK is not added to the code, so calls from a function to S3, DynamoDB or PostgreSQL are not shown as separate steps. That would be the next improvement for performance tuning.

## Free Tier

| Item | Free | Used |
|---|---|---|
| Custom metrics | 10 | 9 |
| Alarms | 10 | 8 (these 7 and the billing alarm) |
| Dashboards | 3 | 1 |
| Logs | 5 GB | A few MB |
| X-Ray traces | 100,000 per month | A few hundred |
| SNS email notifications | 1,000 per month | A few |
