"""Report Lambda: writes the monthly compliance report to the reports bucket.

Trigger: an EventBridge schedule at 06:00 South African time on the 1st of each
month, which reports on the month that has just ended. It can also be invoked
by hand for any month:

    aws lambda invoke --function-name fintrust-capstone-report \
        --cli-binary-format raw-in-base64-out --payload '{"month": "2026-09"}' out.json

Two files are written, with the same numbers in two shapes:

    monthly/2026-09/fintrust-report-2026-09.json   the full report, for systems
    monthly/2026-09/fintrust-report-2026-09.csv    one row per account, for Excel

The analytics are NOT rewritten here. This module runs the project's SQL files
(sql/monthly_summary.sql and sql/risk_report.sql) and the v_open_alerts view,
so the report, the SQL artefacts and the compliance team's own queries can
never disagree. The deploy script copies the two files into the package.

This Lambda runs inside the VPC, logs in as the read-only database user
fintrust_report, and reaches S3 through the free gateway endpoint.

POPIA: the report identifies accounts by account id and customer id only. It
holds no names or contact details. The bucket is encrypted with the project key.

Running it again for the same month replaces the files (the bucket keeps the
earlier versions), so a retry is harmless.
"""

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from fintrust import config, db
from fintrust.log import get_logger

logger = get_logger("report")

# Module level: created once per Lambda container and reused.
_s3 = boto3.client("s3", region_name=config.region())

SAST = timezone(timedelta(hours=2))            # South Africa has no daylight saving
MONTH_PATTERN = re.compile(r"^(20[0-9]{2})-(0[1-9]|1[0-2])$")
KEY_PREFIX = "monthly"

# Where the SQL files are found: inside the package in Lambda, sql/ in the
# repository (see db.read_sql_file).
SQL_DIR = db.SQL_DIR

CSV_COLUMNS = [
    "month", "account_id", "transaction_count", "opening_balance", "total_credits", "total_debits",
    "closing_balance", "high_risk_count", "more_than_3_high_risk_in_30_days",
]

MONTH_TOTALS = """
    SELECT count(*)                                                    AS transactions,
           count(DISTINCT account_id)                                  AS active_accounts,
           COALESCE(sum(amount) FILTER (WHERE tx_type = 'CREDIT'), 0)  AS total_credits,
           COALESCE(sum(amount) FILTER (WHERE tx_type = 'DEBIT'), 0)   AS total_debits,
           count(*) FILTER (WHERE risk_level = 'LOW')                  AS low_risk,
           count(*) FILTER (WHERE risk_level = 'MEDIUM')               AS medium_risk,
           count(*) FILTER (WHERE risk_level = 'HIGH')                 AS high_risk
    FROM transactions
    WHERE tx_date >= %s AND tx_date < %s
"""

RULES_FIRED = """
    SELECT e.event_type AS rule, count(*) AS times_fired
    FROM risk_events e
    JOIN transactions t ON t.transaction_id = e.transaction_id
    WHERE t.tx_date >= %s AND t.tx_date < %s
    GROUP BY e.event_type
    ORDER BY e.event_type
"""

ALERT_TOTALS = """
    SELECT count(*) FILTER (WHERE created_at >= %(start)s AND created_at < %(end)s)    AS raised_in_month,
           count(*) FILTER (WHERE resolved_at >= %(start)s AND resolved_at < %(end)s)  AS resolved_in_month,
           count(*) FILTER (WHERE status = 'OPEN')                                    AS open_now
    FROM alerts
"""

# Wraps sql/monthly_summary.sql: from the running balance per transaction, keep
# one month and reduce it to one row per account. The closing balance is the
# running balance on the account's last transaction of the month.
ACCOUNT_SUMMARY = """
    SELECT s.account_id,
           count(*)                                                        AS transaction_count,
           COALESCE(sum(s.amount) FILTER (WHERE s.tx_type = 'CREDIT'), 0)  AS total_credits,
           COALESCE(sum(s.amount) FILTER (WHERE s.tx_type = 'DEBIT'), 0)   AS total_debits,
           (array_agg(s.running_balance ORDER BY s.tx_date DESC, s.transaction_id DESC))[1] AS closing_balance,
           count(*) FILTER (WHERE t.risk_level = 'HIGH')                   AS high_risk_count
    FROM ({monthly_summary}) AS s
    JOIN transactions t ON t.transaction_id = s.transaction_id
    WHERE s.tx_month = %s
    GROUP BY s.account_id
    ORDER BY s.account_id
"""

OPEN_ALERTS = """
    SELECT alert_id, alert_raised_at, hours_open, transaction_id, account_id, amount, currency,
           country_code, risk_score, rules_fired
    FROM v_open_alerts
"""


class ReportError(Exception):
    """The report could not be produced. Lambda records the failure and retries a scheduled run."""


def previous_month(moment):
    """The calendar month before `moment`, in South African time, as "YYYY-MM"."""
    first_of_this_month = moment.astimezone(SAST).replace(day=1)
    return (first_of_this_month - timedelta(days=1)).strftime("%Y-%m")


def resolve_month(event, now=None):
    """Which month to report on.

    {"month": "2026-09"}   that month (a manual run)
    a scheduled event      the month before the event's time
    anything else          the month before now
    """
    event = event or {}
    month = event.get("month")
    if month is not None:
        if not isinstance(month, str) or not MONTH_PATTERN.match(month):
            raise ReportError(f"month must look like 2026-09, got {month!r}")
        return month
    moment = now or datetime.now(timezone.utc)
    if isinstance(event.get("time"), str):
        try:
            # EventBridge sends the scheduled time. Using it (not "now") means a
            # retry a few minutes later still reports on the same month.
            moment = datetime.fromisoformat(event["time"].replace("Z", "+00:00"))
        except ValueError:
            pass
    return previous_month(moment)


def month_bounds(month):
    """(start, end) of the month in South African time; end is the first moment of the next month."""
    year, number = (int(part) for part in month.split("-"))
    start = datetime(year, number, 1, tzinfo=SAST)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=SAST)
    return start, end


def load_sql(name):
    """Read one of the project's SQL files, without its final semicolon so it can be wrapped."""
    try:
        return db.read_sql_file(name)
    except OSError as error:
        raise ReportError(f"SQL file {name} is missing from the package") from error


def build_report(month, generated_at=None):
    """Run every query and return the report as one dictionary."""
    start, end = month_bounds(month)
    account_sql = ACCOUNT_SUMMARY.format(monthly_summary=load_sql("monthly_summary.sql"))
    risk_sql = load_sql("risk_report.sql")

    # One database transaction, so every section sees the same state of the ledger.
    with db.transaction() as cursor:
        cursor.execute(MONTH_TOTALS, (start, end))
        totals = dict(cursor.fetchone())
        cursor.execute(RULES_FIRED, (start, end))
        rules = {row["rule"]: row["times_fired"] for row in cursor.fetchall()}
        cursor.execute(ALERT_TOTALS, {"start": start, "end": end})
        alerts = dict(cursor.fetchone())
        cursor.execute(account_sql, (month,))
        accounts = [dict(row) for row in cursor.fetchall()]
        cursor.execute(risk_sql)
        high_risk_accounts = [dict(row) for row in cursor.fetchall()]
        cursor.execute(OPEN_ALERTS)
        open_alerts = [dict(row) for row in cursor.fetchall()]

    breached = {row["account_id"] for row in high_risk_accounts}
    for account in accounts:
        net = account["total_credits"] - account["total_debits"]
        account["opening_balance"] = account["closing_balance"] - net
        account["more_than_3_high_risk_in_30_days"] = account["account_id"] in breached

    # Control totals: the per-account rows must add up to the month's totals.
    control_totals_agree = (
        sum(a["transaction_count"] for a in accounts) == totals["transactions"]
        and sum((a["total_credits"] for a in accounts), Decimal(0)) == totals["total_credits"]
        and sum((a["total_debits"] for a in accounts), Decimal(0)) == totals["total_debits"]
        and sum(a["high_risk_count"] for a in accounts) == totals["high_risk"]
    )

    generated_at = generated_at or datetime.now(timezone.utc)
    return {
        "report": "FinTrust monthly transaction and risk report",
        "month": month,
        "period_start": start.isoformat(),
        "period_end_exclusive": end.isoformat(),
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            **totals,
            "net_movement": totals["total_credits"] - totals["total_debits"],
            "rules_fired": rules,
            "alerts": alerts,
            "control_totals_agree": control_totals_agree,
        },
        "accounts": accounts,
        # These two sections describe the whole ledger at the time of generation,
        # not only this month: a rolling 30-day window does not stop at month end.
        "high_risk_accounts": high_risk_accounts,
        "open_alerts": open_alerts,
    }


def to_csv(report):
    """The per-account section as CSV text: one row per account active in the month."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for account in report["accounts"]:
        row = {column: account.get(column) for column in CSV_COLUMNS}
        row["month"] = report["month"]
        row["more_than_3_high_risk_in_30_days"] = "yes" if account["more_than_3_high_risk_in_30_days"] else "no"
        writer.writerow(row)
    return buffer.getvalue()


def report_keys(month):
    """The two object keys for a month."""
    stem = f"{KEY_PREFIX}/{month}/fintrust-report-{month}"
    return f"{stem}.json", f"{stem}.csv"


def store(report):
    """Write both files to the reports bucket and return their keys."""
    bucket = config.get("reports_bucket")
    json_key, csv_key = report_keys(report["month"])
    # default=str turns Decimal and datetime values into text. Money stays exact
    # ("7903.00"), which a float would not guarantee.
    files = (
        (json_key, json.dumps(report, indent=2, default=str).encode("utf-8"), "application/json"),
        (csv_key, to_csv(report).encode("utf-8"), "text/csv"),
    )
    try:
        for key, body, content_type in files:
            _s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    except (ClientError, BotoCoreError) as error:
        raise ReportError(f"Could not write the report to the reports bucket: {error}") from error
    return json_key, csv_key


def handler(event, context):
    """Lambda entry point."""
    log = logger.bind(request_id=getattr(context, "aws_request_id", "local"))
    try:
        month = resolve_month(event)
        log = log.bind(month=month)
        report = build_report(month)
        json_key, csv_key = store(report)
    except (ReportError, db.DatabaseError) as error:
        # db.transaction() has already rolled back. Raising marks the invocation
        # as failed: a scheduled run is retried and the Lambda errors alarm fires.
        log.error("report_failed", error_type=type(error).__name__, error=str(error))
        raise

    summary = report["summary"]
    if not summary["control_totals_agree"]:
        log.error("control_totals_disagree")
    if summary["transactions"] == 0:
        log.warning("report_has_no_transactions")
    result = {
        "month": month,
        "json_key": json_key,
        "csv_key": csv_key,
        "transactions": summary["transactions"],
        "accounts": len(report["accounts"]),
        "high_risk_accounts": len(report["high_risk_accounts"]),
        "open_alerts": len(report["open_alerts"]),
        "control_totals_agree": summary["control_totals_agree"],
    }
    log.info("report_written", **result)
    return result
