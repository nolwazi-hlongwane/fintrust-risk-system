"""Custom CloudWatch metrics, written as log lines (CloudWatch Embedded Metric Format).

A metric is published by PRINTING one JSON line in a special shape. CloudWatch
Logs recognises the "_aws" block and turns the named values into metrics in the
FinTrust/ETL namespace. Nothing else is needed.

Why not call the CloudWatch API (put_metric_data)?
    * The scoring Lambda runs inside the VPC with no NAT Gateway, so it cannot
      reach the CloudWatch API. Its log lines, however, are collected by the
      Lambda service itself and always arrive.
    * No API call means no extra latency, no extra IAM permission and nothing
      that can fail and break the function.
    * The same line is ordinary JSON, so Logs Insights can query it too.

Cost: each metric NAME below is one custom metric. There are 9, inside the
Free Tier's 10. All share one dimension (Environment), which does not add more.
Only names in METRICS are accepted, so a typing mistake cannot quietly create a
new, billable metric.
"""

import json
import os
import time

from fintrust import config

METRICS = (
    "RowsRead",               # ingest: data rows read from an accepted file
    "RowsQueued",             # ingest: valid rows sent to the scoring queue
    "RowsQuarantined",        # ingest: rows rejected by validation
    "FilesRejected",          # ingest: whole files rejected (bad header, not CSV, empty)
    "TransactionsScored",     # score: transactions written to the ledger
    "HighRiskTransactions",   # score: of those, how many were HIGH risk
    "ScoringFailures",        # score: scoring attempts that failed and will be retried
    "AlertsSent",             # alert: emails sent to compliance
    "AlertFailures",          # alert: emails that could not be sent on this attempt
)
DIMENSION = "Environment"


def build(values, now_ms=None):
    """Return the Embedded Metric Format record for {"RowsRead": 15, ...}."""
    unknown = sorted(set(values) - set(METRICS))
    if unknown:
        raise ValueError(f"Unknown metric name(s): {', '.join(unknown)}")
    try:
        namespace = config.get("metrics_namespace")
    except config.ConfigError:
        namespace = config.DEFAULTS["metrics_namespace"]
    record = {
        "_aws": {
            "Timestamp": now_ms if now_ms is not None else int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [[DIMENSION]],
                    "Metrics": [{"Name": name, "Unit": "Count"} for name in values],
                }
            ],
        },
        DIMENSION: os.environ.get("FINTRUST_ENV", "dev"),
        "message": "metrics",
    }
    record.update({name: int(value) for name, value in values.items()})
    return record


def emit(**values):
    """Publish counts, e.g. emit(RowsRead=15, RowsQueued=14, RowsQuarantined=1).

    Zero is published too. An alarm that divides failures by a total needs to
    see "0 failures", not a gap.
    """
    print(json.dumps(build(values)))
