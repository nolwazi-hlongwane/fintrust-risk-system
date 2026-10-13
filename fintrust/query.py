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

Session routes (Week 15 Day 2, secure sign-in):
    POST /session     record a new sign-in (after Cognito has signed the person in)
    GET /session      who is signed in, their role, and whether the session is still open
    DELETE /session   sign out: the session ends, and the same token is refused from then on

Who may call what (Week 15 Day 2)
    API Gateway checks two things before this function runs: the API key (which
    application is calling, for the usage plan) and the Cognito ID token (which
    PERSON is calling). The token's claims arrive in
    event.requestContext.authorizer.claims. This function then checks:
      * the person's session is still open (fintrust/sessions.py, DynamoDB);
      * the person's role, from their Cognito group:
            compliance-analyst  may read everything and upload files
            compliance-officer  may also resolve alerts and flag accounts
    The checks are made here as well as in API Gateway on purpose (defence in
    depth): a mistake in the gateway's settings can never open a route.

This Lambda runs INSIDE the VPC: it reads the ledger in RDS as the database
user fintrust_query, and the event store and session store in DynamoDB through
the gateway endpoint.

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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from fintrust import __version__, config, db, dynamo, sessions, validators
from fintrust.log import get_logger
from fintrust.validators import ACCOUNT_ID_PATTERN, TRANSACTION_ID_PATTERN, mask_account

logger = get_logger("query")

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")
ALERT_STATUSES = ("OPEN", "RESOLVED")
TX_TYPES = ("CREDIT", "DEBIT")
CHANNELS = ("POS", "ONLINE", "ATM", "EFT", "MOBILE", "BRANCH")     # the same list as schema.sql
ACCOUNT_TYPES = ("CHEQUE", "SAVINGS", "CREDIT", "BUSINESS")
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
SAST = timezone(timedelta(hours=2))     # dates in the API are South African calendar days

MAX_UPLOAD_BYTES = 5 * 1024 * 1024      # Lambda accepts at most 6 MB in one request
MAX_REASON_LENGTH = 500
MAX_SEARCH_LENGTH = 60
MAX_AMOUNT = Decimal("999999999999.99")  # the largest NUMERIC(14,2) value
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
USER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,59}$")
COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")

# Roles are Cognito groups. A person in neither group can sign in but can do nothing.
ANALYST = "compliance-analyst"
OFFICER = "compliance-officer"
ROLES = (ANALYST, OFFICER)

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
        "Access-Control-Allow-Headers": "authorization,content-type,x-api-key",
        "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
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


def error_response(status, code, message, request_id, **extra):
    return respond(status, {"error": {"code": code, "message": message, "request_id": request_id, **extra}})


# ----------------------------------------------------------------------------
# who is calling (Week 15 Day 2)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Caller:
    """The signed-in person, read from the Cognito ID token's claims."""

    username: str
    name: str
    roles: tuple
    session_id: str
    signed_in_at: int | None

    @property
    def is_officer(self):
        return OFFICER in self.roles

    def permissions(self):
        """What the dashboard should offer this person. The API enforces the same rules."""
        return {"read": bool(self.roles), "upload": bool(self.roles),
                "resolve_alerts": self.is_officer, "flag_accounts": self.is_officer}


def _groups(value):
    """cognito:groups as a list. API Gateway passes the claim as text, for
    example "compliance-officer" or "a,b" (some versions write "[a b]")."""
    if isinstance(value, list):
        return [str(item) for item in value]
    return [part for part in re.split(r"[\s,\[\]]+", str(value or "")) if part]


def caller_from(event):
    """The Caller for a request, or None if API Gateway passed no identity.

    The claims are trustworthy because API Gateway verified the token's
    signature, issuer and expiry before the request reached this function; a
    client cannot write requestContext itself.
    """
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    username = str(claims.get("cognito:username") or "").strip().lower()
    session_id = str(claims.get("origin_jti") or "").strip()
    if not username or not session_id:
        return None
    if not USER_PATTERN.match(username):
        raise ApiError(403, "FORBIDDEN", "This username cannot be used with the API")
    try:
        signed_in_at = int(claims["auth_time"]) if claims.get("auth_time") else None
    except (TypeError, ValueError):
        signed_in_at = None
    roles = tuple(role for role in ROLES if role in _groups(claims.get("cognito:groups")))
    name = str(claims.get("name") or username).strip()[:120]
    return Caller(username, name, roles, session_id, signed_in_at)


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


def _choice(params, name, allowed, label=None):
    """An optional value that must be one of `allowed` (upper-cased), or None."""
    value = (params.get(name) or "").strip().upper()
    if not value:
        return None
    if value not in allowed:
        raise ApiError(400, "VALIDATION_ERROR", f"{label or name} must be one of {', '.join(allowed)}")
    return value


def _sort(params, sorts, default):
    """The ORDER BY for the `sort` parameter.

    The caller chooses a NAME from a fixed list; the SQL for each name is
    written here. Text from the request is never put into the SQL, so sorting
    cannot be used for SQL injection.
    """
    chosen = (params.get("sort") or default).strip().lower()
    if chosen not in sorts:
        raise ApiError(400, "VALIDATION_ERROR", f"sort must be one of {', '.join(sorts)}")
    return chosen, sorts[chosen]


def _amount(params, name):
    """An optional rand amount (0 or more, at most two decimal places), or None."""
    raw = (params.get(name) or "").strip().replace(" ", "")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be a number, for example 50000 or 1250.50") from None
    if not value.is_finite() or value < 0 or value > MAX_AMOUNT or value != value.quantize(Decimal("0.01")):
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be between 0 and {MAX_AMOUNT} with at most two decimals")
    return value


def _contains(params, name):
    """An optional search text, as a LIKE pattern that matches it anywhere.

    The LIKE wildcards % and _ (and the escape character itself) are escaped,
    so a search for "100%" finds exactly "100%" and cannot match everything.
    """
    raw = (params.get(name) or "").strip()
    if not raw:
        return None
    if len(raw) > MAX_SEARCH_LENGTH:
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be at most {MAX_SEARCH_LENGTH} characters")
    if any(ord(character) < 32 for character in raw):
        raise ApiError(400, "VALIDATION_ERROR", f"{name} contains characters that are not allowed")
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _account_id(params, name="account_id"):
    """An optional account id. "1007" is accepted as shorthand for "ACC-1007"."""
    value = (params.get(name) or "").strip().upper()
    if not value:
        return None
    if value.isdigit():
        value = f"ACC-{value}"
    if not ACCOUNT_ID_PATTERN.match(value):
        raise ApiError(400, "VALIDATION_ERROR", "account_id must look like ACC-1234")
    return value


def _page(params):
    return _integer(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT), _integer(params, "offset", 0, 0, 1_000_000)


def _pagination(limit, offset, items, total):
    return {"limit": limit, "offset": offset, "total": total, "has_more": offset + len(items) < total}


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
def get_health(_event, _caller=None):
    if db.is_healthy():
        return respond(200, {"status": "ok", "database": "ok", "version": __version__})
    return respond(503, {"status": "degraded", "database": "unavailable", "version": __version__})


# Sort orders the dashboard offers. Every order ends with the newest first and
# then the id, so rows that tie (two equal amounts) always come back in the
# same order and paging through them never skips or repeats a row.
_NEWEST = "tx_date DESC, transaction_id DESC"
TRANSACTION_SORTS = {
    "newest": _NEWEST,
    "oldest": "tx_date ASC, transaction_id ASC",
    "amount_desc": f"amount DESC, {_NEWEST}",
    "amount_asc": f"amount ASC, {_NEWEST}",
    "account_asc": f"account_id ASC, {_NEWEST}",
    "account_desc": f"account_id DESC, {_NEWEST}",
    # lower(): "acme" and "Acme" sort together. Rows with no merchant go last either way.
    "merchant_asc": f"lower(merchant_name) ASC NULLS LAST, {_NEWEST}",
    "merchant_desc": f"lower(merchant_name) DESC NULLS LAST, {_NEWEST}",
    "score_desc": f"risk_score DESC, {_NEWEST}",
    "score_asc": f"risk_score ASC, {_NEWEST}",
}


def get_transactions(event, _caller=None):
    params = event.get("queryStringParameters") or {}
    conditions, values = [], []

    def where(condition, value):
        conditions.append(condition)
        values.append(value)

    account_id = _account_id(params)
    if account_id:
        where("account_id = %s", account_id)

    transaction_id = (params.get("transaction_id") or "").strip()
    if transaction_id:
        if not TRANSACTION_ID_PATTERN.match(transaction_id):
            raise ApiError(400, "VALIDATION_ERROR", "transaction_id is not in a valid format")
        where("transaction_id = %s", transaction_id)

    for name, allowed in (("risk_level", RISK_LEVELS), ("tx_type", TX_TYPES), ("channel", CHANNELS)):
        chosen = _choice(params, name, allowed)
        if chosen:
            where(f"{name} = %s", chosen)

    country = (params.get("country_code") or "").strip().upper()
    if country:
        if not COUNTRY_PATTERN.match(country):
            raise ApiError(400, "VALIDATION_ERROR", "country_code must be two letters, for example ZA or GB")
        where("country_code = %s", country)

    minimum, maximum = _amount(params, "min_amount"), _amount(params, "max_amount")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ApiError(400, "VALIDATION_ERROR", "min_amount must not be more than max_amount")
    if minimum is not None:
        where("amount >= %s", minimum)
    if maximum is not None:
        where("amount <= %s", maximum)

    merchant = _contains(params, "merchant")
    if merchant:
        where("merchant_name ILIKE %s ESCAPE '\\'", merchant)

    _start_day, _end_day, start, end = _period(params)
    if start:
        where("tx_date >= %s", start)
    if end:
        where("tx_date < %s", end)

    sort, order_by = _sort(params, TRANSACTION_SORTS, "newest")
    limit, offset = _page(params)
    where_sql = _where(conditions)     # built only from the fixed strings above, never from user input

    with db.transaction() as cursor:
        cursor.execute(f"SELECT count(*) AS total FROM transactions {where_sql}", values)
        total = cursor.fetchone()["total"]
        cursor.execute(
            f"""
            SELECT transaction_id, account_id, tx_date, amount, currency, tx_type, channel,
                   merchant_name, country_code, description, risk_score, risk_level
            FROM transactions {where_sql}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
            """,
            [*values, limit, offset],
        )
        items = [dict(row) for row in cursor.fetchall()]

    return respond(200, {"items": items, "sort": sort, "pagination": _pagination(limit, offset, items, total)})


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


def get_journey(event, _caller=None):
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


def get_insights(event, _caller=None):
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


def get_trends(event, _caller=None):
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


def get_account_balance(event, _caller=None):
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
ACCOUNT_SORTS = {
    "risk_desc": "total_risk_points DESC, account_id",
    "risk_asc": "total_risk_points ASC, account_id",
    "account_asc": "account_id ASC",
    "account_desc": "account_id DESC",
    "recent": "last_transaction_at DESC NULLS LAST, account_id",
    "transactions_desc": "transaction_count DESC, account_id",
    "high_risk_desc": "high_risk_count DESC, total_risk_points DESC, account_id",
    "open_alerts_desc": "open_alerts DESC, total_risk_points DESC, account_id",
}


def get_accounts(event, _caller=None):
    """Accounts ranked by risk, read from the v_risk_dashboard view (sql/views.sql).

    The view holds no customer names or contact details, and the API needs to
    know nothing about the tables behind it.
    """
    params = event.get("queryStringParameters") or {}
    conditions, values = [], []
    status = _choice(params, "status", ACCOUNT_STATUSES)
    if status:
        conditions.append("account_status = %s")
        values.append(status)
    account_type = _choice(params, "account_type", ACCOUNT_TYPES)
    if account_type:
        conditions.append("account_type = %s")
        values.append(account_type)
    search = _contains(params, "search")
    if search:
        conditions.append("account_id ILIKE %s ESCAPE '\\'")
        values.append(search)
    sort, order_by = _sort(params, ACCOUNT_SORTS, "risk_desc")
    limit, offset = _page(params)
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
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
            """,
            [*values, limit, offset],
        )
        items = [dict(row) for row in cursor.fetchall()]

    return respond(200, {"items": items, "sort": sort, "pagination": _pagination(limit, offset, items, total)})


_RAISED_NEWEST = "a.created_at DESC, a.alert_id DESC"
ALERT_SORTS = {
    "newest": _RAISED_NEWEST,
    "oldest": "a.created_at ASC, a.alert_id ASC",
    "amount_desc": f"t.amount DESC, {_RAISED_NEWEST}",
    "amount_asc": f"t.amount ASC, {_RAISED_NEWEST}",
    "score_desc": f"t.risk_score DESC, {_RAISED_NEWEST}",
    "score_asc": f"t.risk_score ASC, {_RAISED_NEWEST}",
    "account_asc": f"t.account_id ASC, {_RAISED_NEWEST}",
    "account_desc": f"t.account_id DESC, {_RAISED_NEWEST}",
}


def get_alerts(event, _caller=None):
    params = event.get("queryStringParameters") or {}
    conditions, values = [], []
    status = _choice(params, "status", ALERT_STATUSES)
    if status:
        conditions.append("a.status = %s")
        values.append(status)
    account_id = _account_id(params)
    if account_id:
        conditions.append("t.account_id = %s")
        values.append(account_id)
    # from/to filter by the day the alert was RAISED.
    _start_day, _end_day, start, end = _period(params)
    if start:
        conditions.append("a.created_at >= %s")
        values.append(start)
    if end:
        conditions.append("a.created_at < %s")
        values.append(end)
    sort, order_by = _sort(params, ALERT_SORTS, "newest")
    limit, offset = _page(params)
    where = _where(conditions)

    with db.transaction() as cursor:
        cursor.execute(
            f"SELECT count(*) AS total FROM alerts a "
            f"JOIN transactions t ON t.transaction_id = a.transaction_id {where}",
            values,
        )
        total = cursor.fetchone()["total"]
        cursor.execute(
            f"""
            SELECT a.alert_id, a.status, a.created_at, a.resolved_at, a.resolved_by, a.resolution_note,
                   t.transaction_id, t.account_id, t.tx_date, t.amount, t.currency, t.country_code,
                   t.risk_score, t.risk_level
            FROM alerts a
            JOIN transactions t ON t.transaction_id = a.transaction_id
            {where}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
            """,
            [*values, limit, offset],
        )
        items = [dict(row) for row in cursor.fetchall()]

    return respond(200, {"items": items, "sort": sort, "pagination": _pagination(limit, offset, items, total)})


def post_upload(event, caller):
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
        # Who sent the file travels with it as object metadata, so the ingest
        # logs and the S3 object itself both say who uploaded it.
        _s3.put_object(Bucket=config.get("raw_bucket"), Key=key, Body=body, ContentType="text/csv",
                       Metadata={"uploaded-by": caller.username})
    except (ClientError, BotoCoreError) as error:
        logger.error("upload_store_failed", error=str(error))
        raise ApiError(503, "SERVICE_UNAVAILABLE", "The file could not be stored. Try again shortly.") from error

    logger.info("upload_accepted", upload_id=upload_id, object_key=key, size_bytes=len(body), actor=caller.username)
    return respond(202, {
        "status": "ACCEPTED",
        "upload_id": upload_id,
        "object_key": key,
        "message": "File accepted. Transactions will appear as they are processed.",
    })


def post_flag(event, caller):
    data = _json_body(event)
    account_id = str(data.get("account_id") or "").strip().upper()
    if not ACCOUNT_ID_PATTERN.match(account_id):
        raise ApiError(400, "VALIDATION_ERROR", "account_id must look like ACC-1234")
    reason = _text_field(data, "reason", MAX_REASON_LENGTH)

    try:
        # One call, one database transaction: the risk event and the status
        # change happen together inside the flag_account function.
        # The officer's name comes from their sign-in, never from the request
        # body, so nobody can flag an account in someone else's name.
        row = db.fetch_one("SELECT flag_account(%s, %s, %s) AS risk_event_id", (account_id, reason, caller.username))
    except db.DatabaseError as error:
        if error.pgcode == ACCOUNT_NOT_FOUND:
            raise ApiError(404, "NOT_FOUND", f"Account {account_id} does not exist") from error
        if error.pgcode == ACCOUNT_CLOSED:
            raise ApiError(409, "CONFLICT", f"Account {account_id} is closed and cannot be flagged") from error
        raise

    logger.info("account_flagged", account=mask_account(account_id), risk_event_id=row["risk_event_id"],
                actor=caller.username)
    return respond(201, {"risk_event_id": row["risk_event_id"], "account_id": account_id, "account_status": "FLAGGED",
                         "flagged_by": caller.username})


def post_resolve(event, caller):
    data = _json_body(event)
    alert_id = data.get("alert_id")
    if isinstance(alert_id, bool) or not isinstance(alert_id, int) or alert_id < 1:
        raise ApiError(400, "VALIDATION_ERROR", "alert_id must be a positive whole number")
    # Who resolved it is the signed-in officer. A resolved_by value in the body
    # (older API clients sent one) is ignored, so the record cannot be forged.
    resolved_by = caller.username
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


# ----------------------------------------------------------------------------
# sessions (Week 15 Day 2)
# ----------------------------------------------------------------------------
def _session_body(caller, item):
    return {**sessions.public_view(item), "permissions": caller.permissions()}


def post_session(event, caller):
    """Record a sign-in. The dashboard calls this once, straight after Cognito
    returns the person to it. Calling it again for the same sign-in is harmless."""
    if not caller.roles:
        raise ApiError(403, "NO_ROLE", "Your sign-in worked, but your account has no role yet. "
                                       "Ask an administrator to add you to the compliance team.")
    headers = event.get("headers") or {}
    user_agent = next((value for name, value in headers.items() if name.lower() == "user-agent"), "")
    item, created = sessions.start(caller.session_id, caller.username, caller.roles, caller.name, user_agent,
                                   caller.signed_in_at)
    return respond(201 if created else 200, _session_body(caller, item))


def get_session(_event, caller):
    """Who is signed in, and whether the session is open. Also the dashboard's
    heartbeat: an active person's session is kept open by this call."""
    return respond(200, _session_body(caller, sessions.require_active(caller.session_id, caller.username)))


def delete_session(_event, caller):
    """Sign out. From now on this token is refused, even though it has not expired."""
    sessions.end(caller.session_id, caller.username, sessions.SIGNED_OUT)
    return respond(200, {"status": sessions.ENDED, "end_reason": sessions.SIGNED_OUT})


ROUTES = {
    ("GET", "/health"): get_health,
    ("POST", "/session"): post_session,
    ("GET", "/session"): get_session,
    ("DELETE", "/session"): delete_session,
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

# Who may call each route. Anything not listed needs an open session and a role.
PUBLIC = "public"              # API key only: a health check reveals nothing
SIGNED_IN = "signed_in"        # a valid sign-in; the session itself is checked by the route
OFFICERS = "officers"          # decisions that change the record
ACCESS = {
    ("GET", "/health"): PUBLIC,
    ("POST", "/session"): SIGNED_IN,
    ("GET", "/session"): SIGNED_IN,
    ("DELETE", "/session"): SIGNED_IN,
    ("POST", "/flag"): OFFICERS,
    ("POST", "/resolve"): OFFICERS,
}
SESSION_MESSAGES = {
    None: "Your session has not started. Sign in again.",
    sessions.SIGNED_OUT: "You signed out. Sign in again to continue.",
    sessions.IDLE: "You were signed out after a period without activity. Sign in again to continue.",
    sessions.EXPIRED: "Your session reached its time limit. Sign in again to continue.",
    sessions.REVOKED: "Your session was ended by an administrator. Sign in again, or contact your administrator.",
}


def authorise(method, resource, caller):
    """Check who is calling before the route runs. Returns the Caller (or None for a public route)."""
    access = ACCESS.get((method, resource))
    if access == PUBLIC:
        return None
    if caller is None:
        raise ApiError(401, "NOT_SIGNED_IN", "Sign in to use the FinTrust API.")
    if access == SIGNED_IN:
        return caller
    if not caller.roles:
        raise ApiError(403, "NO_ROLE",
                       "Your account has no role yet. Ask an administrator to add you to the compliance team.")
    if access == OFFICERS and not caller.is_officer:
        raise ApiError(403, "FORBIDDEN", "Only a compliance officer can do this. Your role is compliance analyst.")
    sessions.require_active(caller.session_id, caller.username)
    return caller


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
            caller = caller_from(event)
            if caller:
                log = log.bind(actor=caller.username)        # every log line says who did it, refused or not
            response = route(event, authorise(method, resource, caller))
    except ApiError as error:
        response = error_response(error.status, error.code, error.message, request_id)
    except sessions.SessionEnded as ended:
        response = error_response(401, "SESSION_ENDED", SESSION_MESSAGES.get(ended.reason, SESSION_MESSAGES[None]),
                                  request_id, reason=ended.reason or "NOT_STARTED")
    except (sessions.SessionError, dynamo.DynamoError):
        response = error_response(503, "SERVICE_UNAVAILABLE", "A service the API depends on is not available. "
                                  "Try again shortly.", request_id)
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
