"""Sign-in sessions, kept in DynamoDB (Week 15 Day 2).

The brief gives DynamoDB three jobs: "session tokens, risk scores, and event
log". This module is the first one.

Why a session record is needed at all
-------------------------------------
A person signs in through Amazon Cognito and the dashboard receives an ID
token: a signed JSON Web Token (JWT) that API Gateway checks on every request.
A JWT cannot be cancelled. Once issued it stays valid until it expires, even if
the person signs out or their laptop is stolen.

So the API also keeps one small record per sign-in, here, and checks it on
every request. Signing out ends the record, and the same token is refused from
that moment, not 30 minutes later. The record is also where the idle time-out
is enforced on the server: a session with no request for 15 minutes ends.

Table fintrust-capstone-sessions
    Partition key  session_id   the token's origin_jti claim. Cognito gives
                                every sign-in its own origin_jti and keeps it
                                when the tokens are refreshed, so one sign-in is
                                one session for its whole life.
    Time-to-live   expires_at   DynamoDB deletes the record 90 days after the
                                sign-in, at no cost (kept that long as an
                                audit trail of who signed in, then removed:
                                POPIA minimality).

A session record holds the username, the role and the times. No token, no
password and no customer data is ever stored here.
"""

import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

from fintrust import config
from fintrust.log import get_logger

logger = get_logger("sessions")

ACTIVE = "ACTIVE"
ENDED = "ENDED"
# Why a session ended. Shown to the person so they know what happened.
SIGNED_OUT = "SIGNED_OUT"         # they chose Sign out
IDLE = "IDLE"                     # no request for session_idle_minutes
EXPIRED = "EXPIRED"               # older than session_max_hours, however active
REVOKED = "REVOKED"               # ended by an administrator (scripts/manage_users.py)
END_REASONS = (SIGNED_OUT, IDLE, EXPIRED, REVOKED)

# last_seen is written at most once a minute: enough for a 15-minute idle rule,
# and it keeps writes (and so cost) low when a page makes several calls at once.
TOUCH_EVERY_SECONDS = 60
USER_AGENT_LENGTH = 160

# Module level: created once per Lambda container and reused.
_dynamodb = boto3.resource("dynamodb", region_name=config.region())
_table = None


class SessionError(Exception):
    """The session store could not be reached."""


class SessionEnded(Exception):
    """The request's session is over. `reason` is one of END_REASONS, or None
    when no session was ever started for this token."""

    def __init__(self, reason):
        super().__init__(reason or "NOT_STARTED")
        self.reason = reason


def table():
    global _table
    if _table is None:
        _table = _dynamodb.Table(config.get("sessions_table"))
    return _table


def reset():
    """Forget the cached table. Used by tests."""
    global _table
    _table = None


def _now():
    return int(time.time())


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(operation, action):
    try:
        return operation()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise
        logger.error("session_store_failed", action=action, error=str(error))
        raise SessionError(f"Session store {action} failed") from error
    except BotoCoreError as error:
        logger.error("session_store_failed", action=action, error=str(error))
        raise SessionError(f"Session store {action} failed") from error


def _limits():
    return (config.get_int("session_idle_minutes") * 60,
            config.get_int("session_max_hours") * 3600,
            config.get_int("session_retention_days") * 86400)


def public_view(item):
    """What the API returns about a session: no internal fields."""
    return {
        "username": item["username"],
        "name": item.get("display_name") or item["username"],
        "roles": list(item.get("roles", [])),
        "status": item["status"],
        "started_at": _iso(int(item["started_epoch"])),
        "last_seen_at": _iso(int(item["last_seen_epoch"])),
        "ends_at": _iso(int(item["ends_epoch"])),
        "idle_minutes": config.get_int("session_idle_minutes"),
        **({"end_reason": item["end_reason"]} if item.get("end_reason") else {}),
    }


def start(session_id, username, roles, display_name=None, user_agent=None, signed_in_epoch=None):
    """Record a new sign-in. Starting the same session twice returns it unchanged.

    A session that has ENDED can never be started again: otherwise a token
    copied before sign-out could simply start a fresh session for itself.
    """
    now = _now()
    _idle, max_age, retention = _limits()
    began = min(int(signed_in_epoch or now), now)
    item = {
        "session_id": session_id,
        "username": username,
        "display_name": (display_name or username)[:120],
        "roles": sorted(roles),
        "status": ACTIVE,
        "started_epoch": began,
        "last_seen_epoch": now,
        "ends_epoch": began + max_age,
        "user_agent": (user_agent or "")[:USER_AGENT_LENGTH],
        "expires_at": began + retention,
    }
    if now >= item["ends_epoch"]:
        raise SessionEnded(EXPIRED)
    try:
        _run(lambda: table().put_item(Item=item, ConditionExpression=Attr("session_id").not_exists()), "start")
    except ClientError:
        # Already recorded: the same sign-in asked twice (for example two tabs
        # opening at once). Return the stored record, and refuse an ended one.
        existing = require_active(session_id, username)
        return existing, False
    logger.info("session_started", username=username, roles=item["roles"])
    return item, True


def require_active(session_id, username):
    """The session record, if it is ACTIVE and inside both time limits.

    Ends the session (and raises SessionEnded) if the idle or absolute limit
    has passed. Refreshes last_seen at most once a minute.
    """
    response = _run(lambda: table().get_item(Key={"session_id": session_id}, ConsistentRead=True), "get")
    item = response.get("Item")
    if item is None or item.get("username") != username:
        raise SessionEnded(None)
    if item["status"] != ACTIVE:
        raise SessionEnded(item.get("end_reason"))

    now = _now()
    idle, _max_age, _retention = _limits()
    if now >= int(item["ends_epoch"]):
        end(session_id, username, EXPIRED)
        raise SessionEnded(EXPIRED)
    if now - int(item["last_seen_epoch"]) > idle:
        end(session_id, username, IDLE)
        raise SessionEnded(IDLE)

    if now - int(item["last_seen_epoch"]) >= TOUCH_EVERY_SECONDS:
        try:
            _run(lambda: table().update_item(
                Key={"session_id": session_id},
                UpdateExpression="SET last_seen_epoch = :now",
                ConditionExpression=Attr("status").eq(ACTIVE),
                ExpressionAttributeValues={":now": now},
            ), "touch")
            item["last_seen_epoch"] = now
        except ClientError:
            # Ended by another request a moment ago (for example Sign out in another tab).
            raise SessionEnded(SIGNED_OUT) from None
    return item


def end(session_id, username, reason):
    """End a session. Ending one that has already ended changes nothing."""
    if reason not in END_REASONS:
        raise ValueError(f"unknown end reason {reason}")
    now = _now()
    try:
        _run(lambda: table().update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET #status = :ended, end_reason = :reason, ended_epoch = :now",
            ConditionExpression=Attr("status").eq(ACTIVE) & Attr("username").eq(username),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":ended": ENDED, ":reason": reason, ":now": now},
        ), "end")
    except ClientError:
        return False                       # missing, already ended, or someone else's
    logger.info("session_ended", username=username, reason=reason)
    return True


def end_all_for_user(username, reason=REVOKED):
    """End every active session of one person. Used when an account is
    disabled or a device is lost. Returns how many sessions were ended.

    A Scan is fine here: it is a rare administrator action on a small table.
    """
    ended, start_key = 0, None
    while True:
        arguments = {"FilterExpression": Attr("username").eq(username) & Attr("status").eq(ACTIVE),
                     "ProjectionExpression": "session_id"}
        if start_key:
            arguments["ExclusiveStartKey"] = start_key
        response = _run(lambda arguments=arguments: table().scan(**arguments), "scan")
        for item in response.get("Items", []):
            ended += end(item["session_id"], username, reason)
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return ended
