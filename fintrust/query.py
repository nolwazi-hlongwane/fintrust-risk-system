"""Query Lambda: the REST API behind API Gateway.

API Gateway checks the API key, then passes the whole HTTP request to this
function as one event ("Lambda proxy integration"). The function decides which
route was called, runs it, and returns the status code, headers and JSON body.

Routes (Week 13 Day 5: the read API):
    GET /health                                  is the API up, can it reach the database?
    GET /transactions                            list, filtered and paginated
    GET /transactions/{transaction_id}/journey   one transaction's steps and score breakdown
    GET /insights                                risk summary for the compliance team

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

import json
import time
from datetime import date, datetime, timedelta, timezone

from fintrust import __version__, config, db, dynamo
from fintrust.log import get_logger
from fintrust.validators import ACCOUNT_ID_PATTERN, TRANSACTION_ID_PATTERN, mask_account

logger = get_logger("query")

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
SAST = timezone(timedelta(hours=2))     # dates in the API are South African calendar days


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


def get_journey(event):
    transaction_id = ((event.get("pathParameters") or {}).get("transaction_id") or "").strip()
    if not TRANSACTION_ID_PATTERN.match(transaction_id):
        raise ApiError(400, "VALIDATION_ERROR", "transaction_id is not in a valid format")

    journey = dynamo.get_journey(transaction_id)
    score, events = journey["score"], journey["events"]
    if not score and not events:
        raise ApiError(404, "NOT_FOUND", f"No transaction with id {transaction_id}")

    account_id = (score or events[0])["account_id"]
    return respond(200, {
        "transaction_id": transaction_id,
        "account_id": account_id,
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


ROUTES = {
    ("GET", "/health"): get_health,
    ("GET", "/transactions"): get_transactions,
    ("GET", "/transactions/{transaction_id}/journey"): get_journey,
    ("GET", "/insights"): get_insights,
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

    account = (event.get("queryStringParameters") or {}).get("account_id")
    log.info(
        "request_completed",
        status=response["statusCode"],
        duration_ms=round((time.monotonic() - started) * 1000),
        **({"account": mask_account(account)} if account else {}),
    )
    return response
