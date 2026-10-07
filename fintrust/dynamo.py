"""DynamoDB event store helpers: put, get, query by account.

Table design (one table, two kinds of item):

    Partition key  account_id     e.g. "ACC-1007"
    Sort key       tx_timestamp   starts with the transaction time in UTC, then
                                  the transaction id, then the kind of record:

        2026-09-15T13:10:00Z#TXN-20260915-000231#SCORE
        2026-09-15T13:10:00Z#TXN-20260915-000231#EVENT#2026-10-06T09:00:01.120417Z#QUEUED

Why the sort key is built like this:
  * It starts with the timestamp, so one account's items are stored in time
    order and "latest activity for this account" is a single cheap Query.
  * The transaction id is added because two transactions on one account can
    share the same second. A timestamp alone would let one overwrite the other.
  * SCORE items hold the risk score and the rules that produced it.
    EVENT items record each step of the transaction's journey.
    ALERT items mark that the alert email for a transaction has been sent.

A global secondary index (by_transaction) lets the API look up one
transaction's items by transaction_id without knowing the account.

No customer names or contact details are ever written to this table.
"""

import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from fintrust import config
from fintrust.log import get_logger

logger = get_logger("dynamo")

TRANSACTION_INDEX = "by_transaction"
RETENTION_DAYS = 365  # items expire after 12 months (DynamoDB time-to-live)

STAGES = (
    "RECEIVED",       # row read from the CSV file
    "VALIDATED",      # passed every validation rule
    "QUEUED",         # sent to the SQS FIFO queue
    "SCORE_FAILED",   # a scoring attempt failed (SQS will retry, then dead-letter)
    "SCORED",         # stored in the ledger with its risk score
    "ALERT_SENT",     # compliance email sent
    "ALERT_FAILED",   # compliance email could not be sent
)

# Module level: created once per Lambda container and reused.
_dynamodb = boto3.resource("dynamodb", region_name=config.region())
_table = None


class DynamoError(Exception):
    """A DynamoDB operation failed."""


def table():
    """The events table. Its name is read from config on first use."""
    global _table
    if _table is None:
        _table = _dynamodb.Table(config.get("dynamo_table"))
    return _table


def reset():
    """Forget the cached table. Used by tests."""
    global _table
    _table = None


def _expires_at():
    return int(time.time()) + RETENTION_DAYS * 24 * 60 * 60


def _now_text():
    # Microseconds, because this time is part of an event's sort key: two events
    # of the same stage written within one millisecond would otherwise share a
    # key, and the second would silently replace the first.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def score_key(tx_timestamp, transaction_id):
    return f"{tx_timestamp}#{transaction_id}#SCORE"


def _clean(item):
    """DynamoDB returns every number as Decimal; turn whole numbers back into int."""
    if isinstance(item, dict):
        return {key: _clean(value) for key, value in item.items()}
    if isinstance(item, list):
        return [_clean(value) for value in item]
    if hasattr(item, "is_finite") and item == item.to_integral_value():
        return int(item)
    return item


def _run(operation, action, **context):
    """Call DynamoDB; log and wrap any failure in DynamoError."""
    try:
        return operation()
    except (ClientError, BotoCoreError) as error:
        logger.error("dynamo_failed", action=action, error=str(error), **context)
        raise DynamoError(f"DynamoDB {action} failed") from error


def put_score(score):
    """Store a RiskScore with its rule breakdown.

    Writing the same score twice simply replaces the item, so a message that
    SQS delivers a second time does no harm.
    """
    item = {
        "account_id": score.account_id,
        "tx_timestamp": score_key(score.tx_timestamp, score.transaction_id),
        "record_type": "SCORE",
        "transaction_id": score.transaction_id,
        "risk_score": score.risk_score,
        "risk_level": score.risk_level,
        "rules": [hit.to_dict() for hit in score.rules],
        "scored_at": score.scored_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": _expires_at(),
    }
    _run(lambda: table().put_item(Item=item), "put_score", transaction_id=score.transaction_id)
    return item


def put_event(account_id, tx_timestamp, transaction_id, stage, detail=None):
    """Record one step of a transaction's journey."""
    if stage not in STAGES:
        raise ValueError(f"Unknown stage '{stage}'. Expected one of {STAGES}")
    at = _now_text()
    item = {
        "account_id": account_id,
        "tx_timestamp": f"{tx_timestamp}#{transaction_id}#EVENT#{at}#{stage}",
        "record_type": "EVENT",
        "transaction_id": transaction_id,
        "stage": stage,
        "at": at,
        "expires_at": _expires_at(),
    }
    if detail:
        item["detail"] = str(detail)[:500]
    _run(lambda: table().put_item(Item=item), "put_event", transaction_id=transaction_id, stage=stage)
    return item


def get_score(account_id, tx_timestamp, transaction_id):
    """Fetch one transaction's score by its full key. Returns a dict or None."""
    key = {"account_id": account_id, "tx_timestamp": score_key(tx_timestamp, transaction_id)}
    response = _run(lambda: table().get_item(Key=key), "get_score", transaction_id=transaction_id)
    item = response.get("Item")
    return _clean(item) if item else None


def alert_key(tx_timestamp, transaction_id):
    return f"{tx_timestamp}#{transaction_id}#ALERT"


def alert_already_sent(account_id, tx_timestamp, transaction_id):
    """True if an alert email has already been sent for this transaction.

    A strongly consistent read, so it sees a marker written a moment ago.
    """
    key = {"account_id": account_id, "tx_timestamp": alert_key(tx_timestamp, transaction_id)}
    response = _run(
        lambda: table().get_item(Key=key, ConsistentRead=True), "alert_already_sent", transaction_id=transaction_id
    )
    return "Item" in response


def mark_alert_sent(account_id, tx_timestamp, transaction_id, message_id):
    """Remember that the alert for this transaction was sent, so it is not sent twice."""
    item = {
        "account_id": account_id,
        "tx_timestamp": alert_key(tx_timestamp, transaction_id),
        "record_type": "ALERT",
        "transaction_id": transaction_id,
        "message_id": message_id,
        "sent_at": _now_text(),
        "expires_at": _expires_at(),
    }
    _run(lambda: table().put_item(Item=item), "mark_alert_sent", transaction_id=transaction_id)
    return item


def query_by_account(account_id, limit=50, newest_first=True, since=None, scores_only=False):
    """Items for one account in time order.

    since        optional UTC text such as "2026-09-01T00:00:00Z"; only items
                 for transactions at or after this time are returned.
    scores_only  return SCORE items only (skip journey events).
    """
    condition = Key("account_id").eq(account_id)
    if since:
        condition = condition & Key("tx_timestamp").gte(since)
    arguments = {
        "KeyConditionExpression": condition,
        "ScanIndexForward": not newest_first,
    }
    if scores_only:
        arguments["FilterExpression"] = Attr("record_type").eq("SCORE")

    # A filter is applied after DynamoDB reads a page, so one page can hold
    # fewer matches than `limit`. Keep reading pages until we have enough.
    items = []
    while len(items) < limit:
        response = _run(lambda: table().query(**arguments), "query_by_account", account_id=account_id)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return [_clean(item) for item in items[:limit]]


def get_journey(transaction_id):
    """Everything known about one transaction: its score and its events in order.

    Returns {"score": dict or None, "events": [dict, ...]}.
    Reads the by_transaction index. An index is updated a moment after the
    table, so a step written in the last second may not appear yet.
    """
    arguments = {
        "IndexName": TRANSACTION_INDEX,
        "KeyConditionExpression": Key("transaction_id").eq(transaction_id),
    }
    items = []
    while True:
        response = _run(lambda: table().query(**arguments), "get_journey", transaction_id=transaction_id)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    items = [_clean(item) for item in items]
    scores = [item for item in items if item["record_type"] == "SCORE"]
    events = sorted((item for item in items if item["record_type"] == "EVENT"), key=lambda e: e["at"])
    return {"score": scores[0] if scores else None, "events": events}
