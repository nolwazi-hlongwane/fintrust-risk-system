# CloudWatch Logs Insights Queries

Every Lambda writes one JSON object per log line, so CloudWatch Logs Insights can filter and summarise on any field. To run a query: CloudWatch console, **Logs Insights**, choose the log group, paste the query, choose a time range, **Run query**.

Log lines never contain customer names, contact details or free-text descriptions.

## Ingest Lambda: log group `/aws/lambda/fintrust-capstone-ingest`

**Every file processed, with its row counts**

```
fields @timestamp, source_key, status, rows_read, rows_valid, rows_quarantined, rows_queued
| filter message = "file_processed"
| sort @timestamp desc
| limit 50
```

**Failed ingestion: files rejected outright, and any error**

```
fields @timestamp, level, message, source_key, reason, error
| filter level = "ERROR"
| sort @timestamp desc
| limit 50
```

**How many rows were quarantined per file**

```
fields @timestamp, source_key, count, quarantine_key
| filter message = "rows_quarantined"
| sort @timestamp desc
```

**Reconciliation: rows in equals rows queued plus rows quarantined**

```
filter message = "file_processed"
| stats sum(rows_read) as rows_in, sum(rows_queued) as queued, sum(rows_quarantined) as quarantined by bin(1h)
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
