"""Query Lambda: the REST API behind API Gateway.

API Gateway checks the API key, then passes the whole HTTP request to this
function as one event ("Lambda proxy integration"). The function decides which
route was called, runs it, and returns the status code, headers and JSON body.

Read routes (Week 13 Day 5):
    GET /health                                  is the API up, can it reach the database?
    GET /transactions                            list, filtered and paginated
    GET /transactions/{transaction_id}/journey   one transaction's steps and score breakdown
    GET /insights                                risk summary for the compliance team
    GET /alerts                                  alerts for high-risk transactions
    GET /accounts                                accounts ranked by risk (Week 14 Day 4)

Chart routes (Week 15 Day 1, dashboard analytics):
    GET /insights/trends                         the series behind the Overview charts
    GET /accounts/{account_id}/balance           one account's running balance (sql/monthly_summary.sql)

Write routes (Week 14 Day 1):
    POST /upload    accept a CSV file; answers 202 at once, processing continues in the background
    POST /flag      flag an account for review (calls the flag_account database function)
    POST /resolve   mark an alert as resolved

This Lambda runs INSIDE the VPC: it reads the ledger in RDS as the database
user fintrust_query, and the event store in DynamoDB through the gateway
endpoint.

Rules followed on every route:
    * Input is validated before any query runs; a bad parameter is a 400 with a
      clear message, never a database error.
    * SQL is always parameterised: values are passed separately from the SQL
      text, so user input can never be executed as SQL.
    * json.dumps(default=str) is used for every response, so Decimal amounts
      and datetimes are written as text with no loss of precision.
    * An unexpected error returns a generic 500 with the request id. The detail
      goes to the logs, never to the caller.
"""

import base64
import json
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from fintrust import __version__, config, db, dynamo, validators
from fintrust.log import get_logger
from fintrust.validators import ACCOUNT_ID_PATTERN, TRANSACTION_ID_PATTERN, mask_account

logger = get_logger("query")

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")
ALERT_STATUSES = ("OPEN", "RESOLVED")
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
SAST = timezone(timedelta(hours=2))     # dates in the API are South African calendar days

MAX_UPLOAD_BYTES = 5 * 1024 * 1024      # Lambda accepts at most 6 MB in one request
MAX_REASON_LENGTH = 500
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
USER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,59}$")

# PostgreSQL error codes raised on purpose by the flag_account function.
ACCOUNT_NOT_FOUND = "P0002"             # no_data_found
ACCOUNT_CLOSED = "55000"                # object_not_in_prerequisite_state

# Module level: created once per Lambda container. Reaches S3 through the VPC's
# gateway endpoint.
_s3 = boto3.client("s3", region_name=config.region())


class ApiError(Exception):
    """An error with an HTTP status that is safe to show the caller."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ----------------------------------------------------------------------------
# responses
# ----------------------------------------------------------------------------
def _headers():
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": config.get("cors_allowed_origin"),
        "Access-Control-Allow-Headers": "content-type,x-api-key",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Cache-Control": "no-store",            # responses contain financial data
        "X-Content-Type-Options": "nosniff",
    }


def allowed_origin(event):
    """CORS: which website may read this response in a browser.

    The setting cors_allowed_origin is the dashboard's address (or several,
    separated by commas). A browser sends the page's address in the Origin
    header; if it is on the list it is echoed back, which is what permits the
    page to read the response. Any other site gets the first allowed address
    back, which does not match its own, so its browser refuses to hand over the
    data. "*" (any site) is only the default before the dashboard is deployed.
    """
    allowed = [origin.strip() for origin in config.get("cors_allowed_origin").split(",") if origin.strip()]
    if not allowed or "*" in allowed:
        return "*"
    headers = event.get("headers") or {}
    origin = next((value for name, value in headers.items() if name.lower() == "origin"), None)
    return origin if origin in allowed else allowed[0]


def respond(status, body):
    return {"statusCode": status, "headers": _headers(), "body": json.dumps(body, default=str)}


def error_response(status, code, message, request_id):
    return respond(status, {"error": {"code": code, "message": message, "request_id": request_id}})


# ----------------------------------------------------------------------------
# input validation
# ----------------------------------------------------------------------------
def _integer(params, name, default, minimum, maximum):
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be a whole number") from None
    if not minimum <= value <= maximum:
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be between {minimum} and {maximum}")
    return value


def _date(params, name):
    raw = params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be a date in the form YYYY-MM-DD") from None


def _period(params):
    """Read from/to and return them as a half-open time range [start, end)."""
    start_day, end_day = _date(params, "from"), _date(params, "to")
    if start_day and end_day and start_day > end_day:
        raise ApiError(400, "VALIDATION_ERROR", "from must not be after to")
    start = datetime.combine(start_day, datetime.min.time(), SAST) if start_day else None
    # "to" is inclusive, so the range ends at the start of the following day.
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), SAST) if end_day else None
    return start_day, end_day, start, end


def _where(conditions):
    return ("WHERE " + " AND ".join(conditions)) if conditions else ""


def _raw_body(event):
    """The request body as bytes (API Gateway may deliver it base64-encoded)."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True)
        except ValueError:
            raise ApiError(400, "VALIDATION_ERROR", "The request body is not valid base64") from None
    return body.encode("utf-8")


def _json_body(event):
    """The request body as a dict, or a 400."""
    try:
        data = json.loads(_raw_body(event).decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "VALIDATION_ERROR", "The request body must be valid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "VALIDATION_ERROR", "The request body must be a JSON object")
    return data


def _text_field(data, name, max_length, required=True):
    """A cleaned free-text field from a JSON body (control characters removed,
    personal identifiers redacted), or a 400."""
    value = data.get(name)
    if value is not None and not isinstance(value, str):
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be text")
    if value is not None and len(value) > max_length:
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be at most {max_length} characters")
    clean, _warnings = validators.sanitise_text(value, name, max_length)
    if required and not clean:
        raise ApiError(400, "VALIDATION_ERROR", f"{name} is required")
    return clean


# ----------------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------------
def get_health(_event):
    if db.is_healthy():
        return respond(200, {"status": "ok", "database": "ok", "version": __version__})
    return respond(503, {"status": "degraded", "database": "unavailable", "version": __version__})


def get_transactions(event):
    params = event.get("queryStringParameters") or {}
    conditions, values = [], []

    account_id = (params.get("account_id") or "").strip().upper()
    if account_id:
        if not ACCOUNT_ID_PATTERN.match(account_id):
            raise ApiError(400, "VALIDATION_ERROR", "account_id must look like ACC-1234")
        conditions.append("account_id = %s")
        values.append(account_id)

    transaction_id = (params.get("transaction_id") or "").strip()
    if transaction_id:
        if not TRANSACTION_ID_PATTERN.match(transaction_id):
            raise ApiError(400, "VALIDATION_ERROR", "transaction_id is not in a valid format")
        conditions.append("transaction_id = %s")
        values.append(transaction_id)

    risk_level = (params.get("risk_level") or "").strip().upper()
    if risk_level:
        if risk_level not in RISK_LEVELS:
            raise ApiError(400, "VALIDATION_ERROR", f"risk_level must be one of {', '.join(RISK_LEVELS)}")
        conditions.append("risk_level = %s")
        values.append(risk_level)

    _start_day, _end_day, start, end = _period(params)
    if start:
        conditions.append("tx_date >= %s")
        values.append(start)
    if end:
        conditions.append("tx_date < %s")
        values.append(end)

    limit = _integer(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _integer(params, "offset", 0, 0, 1_000_000)
    where = _where(conditions)     # built only from the fixed strings above, never from user input

    with db.transaction() as cursor:
        cursor.execute(f"SELECT count(*) AS total FROM transactions {where}", values)
        total = cursor.fetchone()["total"]
        cursor.execute(
            f"""
            SELECT transaction_id, account_id, tx_date, amount, currency, tx_type, channel,
                   merchant_name, country_code, description, risk_score, risk_level
            FROM transactions {where}
            ORDER BY tx_date DESC, transaction_id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, limit, offset],
        )
        items = [dict(row) for row in cursor.fetchall()]

    return respond(200, {
        "items": items,
        "pagination": {"limit": limit, "offset": offset, "total": total, "has_more": offset + len(items) < total},
    })


def journey_status(score, events):
    """One word describing where the transaction is now."""
    stages = [event["stage"] for event in events]
    if score:
        return "ALERTED" if "ALERT_SENT" in stages else "SCORED"
    failures = stages.count("SCORE_FAILED")
    if failures >= config.get_int("max_receive_count"):
        return "DEAD_LETTERED"
    if failures:
        return "RETRYING"
    if "QUEUED" in stages:
        return "QUEUED"
    return "RECEIVED"


def _ledger_score(transaction_id):
    """A transaction's score and rule breakdown read from RDS, or None if it does not exist."""
    with db.transaction() as cursor:
        cursor.execute(
            "SELECT account_id, risk_score, risk_level, created_at FROM transactions WHERE transaction_id = %s",
            (transaction_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            "SELECT event_type, points, reason FROM risk_events WHERE transaction_id = %s "
            "ORDER BY points DESC, event_type",
            (transaction_id,),
        )
        rules = [{"rule": e["event_type"], "points": e["points"], "detail": e["reason"]} for e in cursor.fetchall()]
    return {
        "transaction_id": transaction_id,
        "account_id": row["account_id"],
        "source": "ledger",
        "status": "SCORED",
        "score": {
            "risk_score": row["risk_score"],
            "risk_level": row["risk_level"],
            "rules": rules,
            "scored_at": row["created_at"],
        },
        "events": [],
    }


def get_journey(event):
    transaction_id = ((event.get("pathParameters") or {}).get("transaction_id") or "").strip()
    if not TRANSACTION_ID_PATTERN.match(transaction_id):
        raise ApiError(400, "VALIDATION_ERROR", "transaction_id is not in a valid format")

    journey = dynamo.get_journey(transaction_id)
    score, events = journey["score"], journey["events"]
    if not score and not events:
        # Nothing in the event store. Transactions loaded as opening data (the
        # migrated history) never passed through the pipeline, but the ledger
        # still holds their score and the rules that fired.
        from_ledger = _ledger_score(transaction_id)
        if from_ledger is None:
            raise ApiError(404, "NOT_FOUND", f"No transaction with id {transaction_id}")
        return respond(200, from_ledger)

    account_id = (score or events[0])["account_id"]
    return respond(200, {
        "transaction_id": transaction_id,
        "account_id": account_id,
        "source": "event_store",
        "status": journey_status(score, events),
        "score": {
            "risk_score": score["risk_score"],
            "risk_level": score["risk_level"],
            "rules": score.get("rules", []),
            "scored_at": score.get("scored_at"),
        } if score else None,
        "events": [
            {"stage": item["stage"], "at": item["at"], **({"detail": item["detail"]} if "detail" in item else {})}
            for item in events
        ],
    })


def get_insights(event):
    params = event.get("queryStringParameters") or {}
    start_day, end_day, start, end = _period(params)
    conditions, values = [], []
    if start:
        conditions.append("t.tx_date >= %s")
        values.append(start)
    if end:
        conditions.append("t.tx_date < %s")
        values.append(end)
    where = _where(conditions)

    with db.transaction() as cursor:
        cursor.execute(
            f"""
            SELECT count(*) AS transactions,
                   COALESCE(sum(t.amount), 0) AS total_value,
                   count(DISTINCT t.account_id) AS accounts,
                   count(*) FILTER (WHERE t.risk_level = 'LOW') AS low,
                   count(*) FILTER (WHERE t.risk_level = 'MEDIUM') AS medium,
                   count(*) FILTER (WHERE t.risk_level = 'HIGH') AS high
            FROM transactions t {where}
            """,
            values,
        )
        totals = cursor.fetchone()

        cursor.execute(
            f"""
            SELECT e.event_type, count(*) AS fired
            FROM risk_events e
            JOIN transactions t ON t.transaction_id = e.transaction_id
            {where}
            GROUP BY e.event_type
            """,
            values,
        )
        fired = {row["event_type"]: row["fired"] for row in cursor.fetchall()}

        cursor.execute(
            f"""
            SELECT count(*) AS open_alerts
            FROM alerts a
            JOIN transactions t ON t.transaction_id = a.transaction_id
            {_where([*conditions, "a.status = 'OPEN'"])}
            """,
            values,
        )
        open_alerts = cursor.fetchone()["open_alerts"]

        cursor.execute(
            f"""
            SELECT t.account_id, count(*) AS high_risk_transactions, a.status
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            {_where([*conditions, "t.risk_level = 'HIGH'"])}
            GROUP BY t.account_id, a.status
            ORDER BY count(*) DESC, t.account_id
            LIMIT 5
            """,
            values,
        )
        top_accounts = [dict(row) for row in cursor.fetchall()]

    return respond(200, {
        "period": {"from": start_day, "to": end_day},
        "totals": {
            "transactions": totals["transactions"],
            "total_value": totals["total_value"],
            "accounts": totals["accounts"],
        },
        "by_risk_level": {"LOW": totals["low"], "MEDIUM": totals["medium"], "HIGH": totals["high"]},
        "rules_fired": {rule: fired.get(rule, 0) for rule in ("HIGH_AMOUNT", "HIGH_VELOCITY", "CROSS_BORDER")},
        "open_alerts": open_alerts,
        "top_risk_accounts": top_accounts,
    })


# ----------------------------------------------------------------------------
# chart data (Week 15 Day 1)
#
# Every chart on the Overview is drawn from numbers PostgreSQL works out here.
# The browser only draws them; it never adds anything up itself. So a chart
# can be checked against the same SQL a compliance analyst would run.
# ----------------------------------------------------------------------------
SCORE_BUCKET_SIZE = 10                  # histogram bars: 0-9, 10-19, ... 90-100
RANKED_ACCOUNTS_SHOWN = 15              # ranking is calculated over ALL accounts, then the top ones are returned
MAX_BALANCE_POINTS = 1000               # one account's balance line; older points are left off and flagged
BURST_LIMIT = 3                         # the brief's rule: MORE THAN 3 high-risk transactions in 30 days
LOCAL_DAY = "(%s AT TIME ZONE 'Africa/Johannesburg')"     # a timestamp as a South African calendar time


def choose_granularity(first_day, last_day):
    """How wide each bar of a time chart is: a day, a week or a month.

    Chosen from the length of the period so a chart always has a readable
    number of bars (at most about 62 days, 104 weeks or any number of months).
    """
    days = (last_day - first_day).days + 1
    if days <= 62:
        return "day"
    if days <= 730:
        return "week"
    return "month"


def bucket_start(day, granularity):
    """The first day of the bar that `day` falls in. Weeks start on Monday,
    exactly as PostgreSQL's date_trunc('week', ...) does."""
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    return day


def bucket_starts(first_day, last_day, granularity):
    """Every bar from first_day to last_day, including the empty ones, so a
    quiet week shows as a gap instead of disappearing from the axis."""
    current, last, starts = bucket_start(first_day, granularity), bucket_start(last_day, granularity), []
    while current <= last:
        starts.append(current)
        if granularity == "day":
            current += timedelta(days=1)
        elif granularity == "week":
            current += timedelta(days=7)
        else:
            current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return starts


def _between(column, start, end):
    """Period conditions for one timestamp column, and their values."""
    conditions, values = [], []
    if start:
        conditions.append(f"{column} >= %s")
        values.append(start)
    if end:
        conditions.append(f"{column} < %s")
        values.append(end)
    return conditions, values


def _chart_span(cursor, start_day, end_day, start, end):
    """The first and last calendar day the time charts cover.

    The dates the user chose, if any. A missing end is filled from the data:
    the earliest and latest transaction or alert activity in the period.
    """
    if start_day and end_day:
        return start_day, end_day
    tx_conditions, tx_values = _between("tx_date", start, end)
    raised_conditions, raised_values = _between("created_at", start, end)
    resolved_conditions, resolved_values = _between("resolved_at", start, end)
    cursor.execute(
        f"""
        SELECT min(moment)::date AS first_day, max(moment)::date AS last_day
        FROM (
            SELECT {LOCAL_DAY % "tx_date"} AS moment FROM transactions {_where(tx_conditions)}
            UNION ALL
            SELECT {LOCAL_DAY % "created_at"} FROM alerts {_where(raised_conditions)}
            UNION ALL
            SELECT {LOCAL_DAY % "resolved_at"} FROM alerts {_where([*resolved_conditions, "resolved_at IS NOT NULL"])}
        ) AS activity
        """,
        [*tx_values, *raised_values, *resolved_values],
    )
    found = cursor.fetchone()
    first_day = start_day or found["first_day"]
    last_day = end_day or found["last_day"]
    return first_day, last_day


def get_trends(event):
    """GET /insights/trends: the four series behind the Overview charts.

    activity            transactions per day/week/month, split Low/Medium/High, with their value
    score_distribution  how many transactions got each score, with the thresholds that cut them
    alert_flow          alerts raised and resolved per day/week/month, and the median time to resolve
    account_ranking     every account ranked by total risk points (PERCENT_RANK, as in
                        sql/risk_report.sql), and whether it broke the 30-day burst rule
    """
    params = event.get("queryStringParameters") or {}
    start_day, end_day, start, end = _period(params)
    tx_conditions, tx_values = _between("t.tx_date", start, end)
    tx_where = _where(tx_conditions)

    with db.transaction() as cursor:
        first_day, last_day = _chart_span(cursor, start_day, end_day, start, end)
        granularity = choose_granularity(first_day, last_day) if first_day and last_day else "day"
        starts = bucket_starts(first_day, last_day, granularity) if first_day and last_day else []

        # 1. Activity. date_trunc puts every transaction in its bar; the unit is
        #    one of three fixed words chosen above, passed as a value, never text from the caller.
        cursor.execute(
            f"""
            SELECT date_trunc(%s, {LOCAL_DAY % "t.tx_date"})::date AS period_start,
                   count(*) FILTER (WHERE t.risk_level = 'LOW')    AS low,
                   count(*) FILTER (WHERE t.risk_level = 'MEDIUM') AS medium,
                   count(*) FILTER (WHERE t.risk_level = 'HIGH')   AS high,
                   COALESCE(sum(t.amount), 0)                      AS total_value
            FROM transactions t {tx_where}
            GROUP BY 1
            """,
            [granularity, *tx_values],
        )
        activity_rows = {row["period_start"]: row for row in cursor.fetchall()}

        # 2. Score distribution: integer division puts a score of 0-9 in bar 0,
        #    10-19 in bar 1 ... and 90-100 in bar 9. Each bar is split by the risk
        #    level stored when the transaction was scored.
        cursor.execute(
            f"""
            SELECT LEAST(t.risk_score / %s, 9) AS bucket, t.risk_level, count(*) AS transactions
            FROM transactions t {tx_where}
            GROUP BY 1, 2
            """,
            [SCORE_BUCKET_SIZE, *tx_values],
        )
        score_rows = cursor.fetchall()

        # 3. Alert flow: when alerts were raised, and when they were resolved.
        raised_conditions, raised_values = _between("created_at", start, end)
        cursor.execute(
            f"""
            SELECT date_trunc(%s, {LOCAL_DAY % "created_at"})::date AS period_start, count(*) AS alerts
            FROM alerts {_where(raised_conditions)}
            GROUP BY 1
            """,
            [granularity, *raised_values],
        )
        raised = {row["period_start"]: row["alerts"] for row in cursor.fetchall()}
        resolved_conditions, resolved_values = _between("resolved_at", start, end)
        resolved_where = _where([*resolved_conditions, "status = 'RESOLVED'"])
        cursor.execute(
            f"""
            SELECT date_trunc(%s, {LOCAL_DAY % "resolved_at"})::date AS period_start, count(*) AS alerts
            FROM alerts {resolved_where}
            GROUP BY 1
            """,
            [granularity, *resolved_values],
        )
        resolved = {row["period_start"]: row["alerts"] for row in cursor.fetchall()}
        cursor.execute(
            f"""
            SELECT round((percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM resolved_at - created_at)) / 3600)::numeric, 1)
                   AS median_hours_to_resolve,
                   (SELECT count(*) FROM alerts WHERE status = 'OPEN') AS open_now
            FROM alerts {resolved_where}
            """,
            resolved_values,
        )
        alert_totals = cursor.fetchone()

        # 4. Ranking. The same two ideas as sql/risk_report.sql, over the chosen period:
        #    PERCENT_RANK() places each account between 0 (lowest) and 1 (highest) by
        #    total risk points, calculated over ALL accounts BEFORE the top ones are
        #    kept; and a rolling 30-day window finds the most high-risk transactions
        #    the account had in any 30 days.
        cursor.execute(
            f"""
            WITH scoped AS (
                SELECT t.* FROM transactions t {tx_where}
            ),
            rolling AS (
                SELECT account_id,
                       COUNT(*) OVER (
                           PARTITION BY account_id
                           ORDER BY tx_date
                           RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW
                       ) AS high_risk_in_30_days
                FROM scoped
                WHERE risk_level = 'HIGH'
            ),
            peaks AS (
                SELECT account_id, MAX(high_risk_in_30_days) AS peak_high_risk_in_30_days
                FROM rolling
                GROUP BY account_id
            ),
            account_totals AS (
                SELECT account_id,
                       COUNT(*)                                     AS transaction_count,
                       COUNT(*) FILTER (WHERE risk_level = 'HIGH')  AS high_risk_count,
                       SUM(risk_score)                              AS total_risk_points
                FROM scoped
                GROUP BY account_id
            ),
            ranked AS (
                SELECT account_totals.*,
                       ROUND(PERCENT_RANK() OVER (ORDER BY total_risk_points)::numeric, 4) AS fraud_percentile,
                       RANK() OVER (ORDER BY total_risk_points DESC)                       AS fraud_rank,
                       COUNT(*) OVER ()                                                    AS accounts_ranked
                FROM account_totals
            )
            SELECT r.account_id, a.status AS account_status, r.transaction_count, r.high_risk_count,
                   r.total_risk_points, r.fraud_rank, r.fraud_percentile, r.accounts_ranked,
                   COALESCE(p.peak_high_risk_in_30_days, 0) AS peak_high_risk_in_30_days
            FROM ranked r
            JOIN accounts a ON a.account_id = r.account_id
            LEFT JOIN peaks p ON p.account_id = r.account_id
            ORDER BY r.fraud_rank, r.account_id
            LIMIT %s
            """,
            [*tx_values, RANKED_ACCOUNTS_SHOWN],
        )
        ranking = [dict(row) for row in cursor.fetchall()]

    activity = []
    for period_start in starts:
        row = activity_rows.get(period_start) or {}
        counts = {level: row.get(level, 0) for level in ("low", "medium", "high")}
        activity.append({
            "period_start": period_start,
            **counts,
            "transactions": sum(counts.values()),
            "total_value": row.get("total_value", 0),
        })

    buckets = [
        {"from": index * SCORE_BUCKET_SIZE, "to": 100 if index == 9 else index * SCORE_BUCKET_SIZE + 9,
         "low": 0, "medium": 0, "high": 0}
        for index in range(10)
    ]
    for row in score_rows:
        buckets[row["bucket"]][row["risk_level"].lower()] += row["transactions"]

    accounts_ranked = ranking[0]["accounts_ranked"] if ranking else 0
    for row in ranking:
        del row["accounts_ranked"]
        row["burst_detected"] = row["peak_high_risk_in_30_days"] > BURST_LIMIT

    return respond(200, {
        "period": {"from": start_day, "to": end_day},
        "span": {"first_day": first_day, "last_day": last_day},
        "granularity": granularity,
        "activity": activity,
        "score_distribution": {
            "bucket_size": SCORE_BUCKET_SIZE,
            "thresholds": {"medium": config.get_int("medium_risk_score"), "high": config.get_int("high_risk_score")},
            "buckets": buckets,
        },
        "alert_flow": {
            "periods": [
                {"period_start": s, "raised": raised.get(s, 0), "resolved": resolved.get(s, 0)} for s in starts
            ],
            "raised": sum(raised.values()),
            "resolved": sum(resolved.values()),
            "open_now": alert_totals["open_now"],
            "median_hours_to_resolve": alert_totals["median_hours_to_resolve"],
        },
        "account_ranking": {
            "accounts_ranked": accounts_ranked,
            "burst_rule": f"more than {BURST_LIMIT} high-risk transactions in any 30 days",
            "items": ranking,
        },
    })


# Wraps sql/monthly_summary.sql, the project's running-balance window function.
# The filters are applied OUTSIDE the window: the balance on each row is worked
# out from the account's whole history first, and only then are rows outside
# the period dropped. Filtering by date first would restart the balance at zero.
# PostgreSQL may still apply "account_id = ..." before the window, because the
# window is partitioned by account_id: the result is the same and only that
# account's rows are read.
BALANCE_SQL = """
    SELECT s.transaction_id, s.tx_date, s.tx_type, s.amount, s.signed_amount, s.running_balance,
           t.risk_level, t.risk_score
    FROM ({monthly_summary}) AS s
    JOIN transactions t ON t.transaction_id = s.transaction_id
    {where}
    ORDER BY s.tx_date DESC, s.transaction_id DESC
    LIMIT %s
"""


def get_account_balance(event):
    """GET /accounts/{account_id}/balance: the account's running balance, one point per transaction."""
    account_id = ((event.get("pathParameters") or {}).get("account_id") or "").strip().upper()
    if not ACCOUNT_ID_PATTERN.match(account_id):
        raise ApiError(400, "VALIDATION_ERROR", "account_id must look like ACC-1234")
    params = event.get("queryStringParameters") or {}
    start_day, end_day, start, end = _period(params)
    period_conditions, period_values = _between("s.tx_date", start, end)
    sql = BALANCE_SQL.format(
        monthly_summary=db.read_sql_file("monthly_summary.sql"),
        where=_where(["s.account_id = %s", *period_conditions]),
    )

    with db.transaction() as cursor:
        cursor.execute("SELECT status FROM accounts WHERE account_id = %s", (account_id,))
        account = cursor.fetchone()
        if account is None:
            raise ApiError(404, "NOT_FOUND", f"Account {account_id} does not exist")
        # One extra row is asked for, only to know whether there were more than the limit.
        cursor.execute(sql, [account_id, *period_values, MAX_BALANCE_POINTS + 1])
        rows = [dict(row) for row in cursor.fetchall()]

    truncated = len(rows) > MAX_BALANCE_POINTS
    points = list(reversed(rows[:MAX_BALANCE_POINTS]))         # oldest first, for drawing left to right
    summary = None
    if points:
        summary = {
            # The balance just before the first point shown: its balance minus its own amount.
            "opening_balance": points[0]["running_balance"] - points[0]["signed_amount"],
            "closing_balance": points[-1]["running_balance"],
            "lowest_balance": min(point["running_balance"] for point in points),
            "highest_balance": max(point["running_balance"] for point in points),
            "credits": sum((p["amount"] for p in points if p["tx_type"] == "CREDIT"), Decimal(0)),
            "debits": sum((p["amount"] for p in points if p["tx_type"] == "DEBIT"), Decimal(0)),
            "high_risk_transactions": sum(1 for point in points if point["risk_level"] == "HIGH"),
        }
    return respond(200, {
        "account_id": account_id,
        "account_status": account["status"],
        "period": {"from": start_day, "to": end_day},
        "points": points,
        "truncated": truncated,
        "summary": summary,
    })


ACCOUNT_STATUSES = ("ACTIVE", "FLAGGED", "CLOSED")


def get_accounts(event):
    """Accounts ranked by risk, read from the v_risk_dashboard view (sql/views.sql).

    The view holds no customer names or contact details, and the API needs to
    know nothing about the tables behind it.
    """
    params = event.get("queryStringParameters") or {}
    conditions, values = [], []
    status = (params.get("status") or "").strip().upper()
    if status:
        if status not in ACCOUNT_STATUSES:
            raise ApiError(400, "VALIDATION_ERROR", f"status must be one of {', '.join(ACCOUNT_STATUSES)}")
        conditions.append("account_status = %s")
        values.append(status)
    limit = _integer(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _integer(params, "offset", 0, 0, 1_000_000)
    where = _where(conditions)

    with db.transaction() as cursor:
        cursor.execute(f"SELECT count(*) AS total FROM v_risk_dashboard {where}", values)
        total = cursor.fetchone()["total"]
        cursor.execute(
            f"""
            SELECT account_id, account_type, account_status, transaction_count, total_credits, total_debits,
                   high_risk_count, medium_risk_count, highest_risk_score, total_risk_points,
                   last_transaction_at, last_high_risk_at, open_alerts, resolved_alerts, manual_flags
            FROM v_risk_dashboard {where}
            ORDER BY total_risk_points DESC, account_id
            LIMIT %s OFFSET %s
            """,
            [*values, limit, offset],
        )
        items = [dict(row) for row in cursor.fetchall()]

    return respond(200, {
        "items": items,
        "pagination": {"limit": limit, "offset": offset, "total": total, "has_more": offset + len(items) < total},
    })


def get_alerts(event):
    params = event.get("queryStringParameters") or {}
    conditions, values = [], []
    status = (params.get("status") or "").strip().upper()
    if status:
        if status not in ALERT_STATUSES:
            raise ApiError(400, "VALIDATION_ERROR", f"status must be one of {', '.join(ALERT_STATUSES)}")
        conditions.append("a.status = %s")
        values.append(status)
    limit = _integer(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _integer(params, "offset", 0, 0, 1_000_000)
    where = _where(conditions)

    with db.transaction() as cursor:
        cursor.execute(f"SELECT count(*) AS total FROM alerts a {where}", values)
        total = cursor.fetchone()["total"]
        cursor.execute(
            f"""
            SELECT a.alert_id, a.status, a.created_at, a.resolved_at, a.resolved_by, a.resolution_note,
                   t.transaction_id, t.account_id, t.tx_date, t.amount, t.currency, t.country_code,
                   t.risk_score, t.risk_level
            FROM alerts a
            JOIN transactions t ON t.transaction_id = a.transaction_id
            {where}
            ORDER BY a.created_at DESC, a.alert_id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, limit, offset],
        )
        items = [dict(row) for row in cursor.fetchall()]

    return respond(200, {
        "items": items,
        "pagination": {"limit": limit, "offset": offset, "total": total, "has_more": offset + len(items) < total},
    })


def post_upload(event):
    """Accept a CSV file and return 202 immediately.

    The file is stored in the raw bucket. S3 then invokes the ingest Lambda
    ASYNCHRONOUSLY (the "Event" invocation type): this function does not wait
    for the file to be processed, so the caller gets an answer in milliseconds
    however large the file is. Progress is visible through the journey route.
    """
    body = _raw_body(event)
    if not body.strip():
        raise ApiError(400, "VALIDATION_ERROR", "The request body is empty; send the CSV file as the body")
    if len(body) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "The file is larger than 5 MB; split it into smaller files")
    try:
        first_line = body.decode("utf-8-sig").splitlines()[0]
    except UnicodeDecodeError:
        raise ApiError(400, "VALIDATION_ERROR", "The file must be UTF-8 text") from None
    problems = validators.validate_header(first_line.split(","))
    if problems:
        raise ApiError(400, "VALIDATION_ERROR", "The first line must be the CSV header: " + "; ".join(problems))

    params = event.get("queryStringParameters") or {}
    filename = (params.get("filename") or "upload.csv").strip()
    if not FILENAME_PATTERN.match(filename):
        raise ApiError(400, "VALIDATION_ERROR",
                       "filename may contain only letters, digits, dots, dashes and underscores")
    if not filename.lower().endswith(".csv"):
        filename += ".csv"

    upload_id = str(uuid.uuid4())
    # The key is built here, never taken from the caller, so a request cannot
    # overwrite another file or write outside the uploads/ prefix.
    key = f"uploads/{datetime.now(SAST):%Y-%m-%d}/{upload_id[:8]}-{filename}"
    try:
        _s3.put_object(Bucket=config.get("raw_bucket"), Key=key, Body=body, ContentType="text/csv")
    except (ClientError, BotoCoreError) as error:
        logger.error("upload_store_failed", error=str(error))
        raise ApiError(503, "SERVICE_UNAVAILABLE", "The file could not be stored. Try again shortly.") from error

    logger.info("upload_accepted", upload_id=upload_id, object_key=key, size_bytes=len(body))
    return respond(202, {
        "status": "ACCEPTED",
        "upload_id": upload_id,
        "object_key": key,
        "message": "File accepted. Transactions will appear as they are processed.",
    })


def post_flag(event):
    data = _json_body(event)
    account_id = str(data.get("account_id") or "").strip().upper()
    if not ACCOUNT_ID_PATTERN.match(account_id):
        raise ApiError(400, "VALIDATION_ERROR", "account_id must look like ACC-1234")
    reason = _text_field(data, "reason", MAX_REASON_LENGTH)

    try:
        # One call, one database transaction: the risk event and the status
        # change happen together inside the flag_account function.
        row = db.fetch_one("SELECT flag_account(%s, %s) AS risk_event_id", (account_id, reason))
    except db.DatabaseError as error:
        if error.pgcode == ACCOUNT_NOT_FOUND:
            raise ApiError(404, "NOT_FOUND", f"Account {account_id} does not exist") from error
        if error.pgcode == ACCOUNT_CLOSED:
            raise ApiError(409, "CONFLICT", f"Account {account_id} is closed and cannot be flagged") from error
        raise

    logger.info("account_flagged", account=mask_account(account_id), risk_event_id=row["risk_event_id"])
    return respond(201, {"risk_event_id": row["risk_event_id"], "account_id": account_id, "account_status": "FLAGGED"})


def post_resolve(event):
    data = _json_body(event)
    alert_id = data.get("alert_id")
    if isinstance(alert_id, bool) or not isinstance(alert_id, int) or alert_id < 1:
        raise ApiError(400, "VALIDATION_ERROR", "alert_id must be a positive whole number")
    resolved_by = str(data.get("resolved_by") or "").strip()
    if not USER_PATTERN.match(resolved_by):
        raise ApiError(400, "VALIDATION_ERROR",
                       "resolved_by must be 2 to 60 letters, digits, dots, dashes or underscores")
    note = _text_field(data, "resolution_note", MAX_REASON_LENGTH, required=False)

    with db.transaction() as cursor:
        # "AND status = 'OPEN'" makes this safe if two people resolve the same
        # alert at once: only one UPDATE matches a row.
        cursor.execute(
            """
            UPDATE alerts
            SET status = 'RESOLVED', resolved_at = now(), resolved_by = %s, resolution_note = %s
            WHERE alert_id = %s AND status = 'OPEN'
            RETURNING alert_id, status, resolved_at, resolved_by
            """,
            (resolved_by, note, alert_id),
        )
        updated = cursor.fetchone()
        if updated is None:
            cursor.execute("SELECT status FROM alerts WHERE alert_id = %s", (alert_id,))
            existing = cursor.fetchone()

    if updated is None:
        if existing is None:
            raise ApiError(404, "NOT_FOUND", f"No alert with id {alert_id}")
        raise ApiError(409, "CONFLICT", f"Alert {alert_id} is already resolved")

    logger.info("alert_resolved", alert_id=alert_id, resolved_by=resolved_by)
    return respond(200, dict(updated))


ROUTES = {
    ("GET", "/health"): get_health,
    ("GET", "/transactions"): get_transactions,
    ("GET", "/transactions/{transaction_id}/journey"): get_journey,
    ("GET", "/insights"): get_insights,
    ("GET", "/alerts"): get_alerts,
    ("POST", "/upload"): post_upload,
    ("GET", "/accounts"): get_accounts,
    ("GET", "/insights/trends"): get_trends,
    ("GET", "/accounts/{account_id}/balance"): get_account_balance,
    ("POST", "/flag"): post_flag,
    ("POST", "/resolve"): post_resolve,
}


def handler(event, context):
    """Lambda entry point. `event` is an API Gateway proxy request."""
    started = time.monotonic()
    request_id = (event.get("requestContext") or {}).get("requestId") or getattr(context, "aws_request_id", "local")
    method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    log = logger.bind(request_id=request_id, method=method, resource=resource)

    try:
        if method == "OPTIONS":
            # Browser "preflight" check before a cross-origin request.
            response = {"statusCode": 204, "headers": _headers(), "body": ""}
        else:
            route = ROUTES.get((method, resource))
            if route is None:
                raise ApiError(404, "NOT_FOUND", f"No route {method} {resource}")
            response = route(event)
    except ApiError as error:
        response = error_response(error.status, error.code, error.message, request_id)
    except db.DatabaseUnavailable:
        response = error_response(503, "SERVICE_UNAVAILABLE", "The database is not available. Try again shortly.",
                                  request_id)
    except Exception as error:
        # db.transaction() has already rolled back. Log the detail; tell the caller nothing internal.
        log.error("unhandled_error", error_type=type(error).__name__, error=str(error))
        response = error_response(500, "INTERNAL_ERROR", "Something went wrong. Quote the request_id to support.",
                                  request_id)

    # Set here, in one place, so every response (success or error) carries it.
    response["headers"]["Access-Control-Allow-Origin"] = allowed_origin(event)
    response["headers"]["Vary"] = "Origin"      # a cache must not reuse one site's answer for another

    account = (event.get("queryStringParameters") or {}).get("account_id")
    log.info(
        "request_completed",
        status=response["statusCode"],
        duration_ms=round((time.monotonic() - started) * 1000),
        **({"account": mask_account(account)} if account else {}),
    )
    return response
