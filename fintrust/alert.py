"""Alert Lambda: emails the compliance team when a transaction is scored HIGH.

Trigger: the DynamoDB stream of the events table. Every time the scoring Lambda
writes a score, DynamoDB adds a record to the stream, and the Lambda service
invokes this function with it. An event filter on the trigger passes on only
SCORE items whose risk_level is HIGH, so this function is not even invoked for
the great majority of transactions.

Why a stream instead of the scoring Lambda calling this one directly?
    * The scoring Lambda is inside the VPC with no NAT Gateway, so it cannot
      reach the Lambda or SES services. It CAN reach DynamoDB (gateway endpoint).
    * It keeps the two jobs independent: if email is failing, scoring and the
      ledger are completely unaffected, and the alert is retried on its own.

This Lambda runs OUTSIDE the VPC: it needs SES and never touches the database.

Exactly one email per transaction:
    Before sending, it checks DynamoDB for an ALERT marker for the transaction;
    after sending, it writes one. A score that is written again (for example
    after a message is redelivered) therefore does not send a second email.
    If the function crashes between sending and writing the marker, the retry
    sends again: for a compliance alert, twice is acceptable and never is not.

POPIA: the email contains the transaction id, a MASKED account id, the score
and the rules that fired. No customer name, contact detail or full account
number leaves the system.
"""

from html import escape

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from fintrust import config, dynamo
from fintrust.log import get_logger
from fintrust.validators import mask_account

logger = get_logger("alert")

# Module level: created once per Lambda container and reused.
_ses = boto3.client("ses", region_name=config.region())
_deserializer = TypeDeserializer()


class AlertError(Exception):
    """At least one alert could not be sent. The stream will retry the batch."""


def _plain(image):
    """Convert a stream image ({"risk_score": {"N": "70"}, ...}) into a normal dict."""
    return {key: _deserializer.deserialize(value) for key, value in image.items()}


def build_email(score):
    """Return (subject, text_body, html_body) for one HIGH risk score item."""
    transaction_id = score["transaction_id"]
    account = mask_account(score["account_id"])
    points = int(score["risk_score"])
    rules = score.get("rules", [])

    subject = f"[FinTrust] HIGH risk transaction {transaction_id} (score {points})"

    lines = [
        "A transaction has been scored HIGH risk and needs review.",
        "",
        f"Transaction : {transaction_id}",
        f"Account     : {account}",
        f"Risk score  : {points} of 100 (HIGH)",
        f"Scored at   : {score.get('scored_at', 'unknown')} UTC",
        "",
        "Why it was flagged:",
    ]
    lines += [f"  +{int(rule['points']):>3}  {rule['rule']}: {rule['detail']}" for rule in rules]
    lines += [
        "",
        "An alert has been opened. Review it on the FinTrust compliance dashboard and resolve it once checked.",
        "",
        "This is an automated message from the FinTrust Transaction Insights and Risk Monitoring System.",
        "The account number is masked on purpose. Do not forward customer details by email.",
    ]
    text = "\n".join(lines)

    scored_at = escape(str(score.get("scored_at", "unknown")))
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0'><b>+{int(rule['points'])}</b></td>"
        f"<td style='padding:4px 12px 4px 0'>{escape(str(rule['rule']))}</td>"
        f"<td style='padding:4px 0'>{escape(str(rule['detail']))}</td></tr>"
        for rule in rules
    )
    html = (
        "<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1a1a1a'>"
        "<p>A transaction has been scored <b style='color:#b42318'>HIGH risk</b> and needs review.</p>"
        "<table style='border-collapse:collapse'>"
        f"<tr><td style='padding:2px 16px 2px 0'>Transaction</td><td><b>{escape(transaction_id)}</b></td></tr>"
        f"<tr><td style='padding:2px 16px 2px 0'>Account</td><td>{escape(account)}</td></tr>"
        f"<tr><td style='padding:2px 16px 2px 0'>Risk score</td><td><b>{points}</b> of 100 (HIGH)</td></tr>"
        f"<tr><td style='padding:2px 16px 2px 0'>Scored at</td><td>{scored_at} UTC</td></tr>"
        "</table>"
        "<p><b>Why it was flagged</b></p>"
        f"<table style='border-collapse:collapse'>{rows}</table>"
        "<p>An alert has been opened. Review it on the FinTrust compliance dashboard and resolve it once checked.</p>"
        "<p style='color:#667085;font-size:12px'>Automated message from the FinTrust Transaction Insights and Risk "
        "Monitoring System. The account number is masked on purpose. Do not forward customer details by email.</p>"
        "</div>"
    )
    return subject, text, html


def send_alert(score):
    """Send the email through SES and return the SES message id."""
    subject, text, html = build_email(score)
    response = _ses.send_email(
        Source=config.get("alert_sender"),
        Destination={"ToAddresses": [config.get("alert_recipient")]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text, "Charset": "UTF-8"}, "Html": {"Data": html, "Charset": "UTF-8"}},
        },
    )
    return response["MessageId"]


def process_record(record, log):
    """Handle one stream record. Returns "sent", "duplicate" or "ignored". Raises if sending fails."""
    if record.get("eventName") not in ("INSERT", "MODIFY"):
        return "ignored"                      # a deletion (for example an expired item)
    score = _plain(record.get("dynamodb", {}).get("NewImage") or {})
    # The trigger's filter already does this; checked again so the function is
    # correct even if the filter is ever changed or removed.
    if score.get("record_type") != "SCORE" or score.get("risk_level") != "HIGH":
        return "ignored"

    transaction_id = score["transaction_id"]
    account_id = score["account_id"]
    tx_timestamp = score["tx_timestamp"].split("#", 1)[0]     # the sort key starts with the timestamp
    log = log.bind(transaction_id=transaction_id)

    if dynamo.alert_already_sent(account_id, tx_timestamp, transaction_id):
        log.info("alert_already_sent")
        return "duplicate"

    try:
        message_id = send_alert(score)
    except (ClientError, BotoCoreError) as error:
        log.error("alert_send_failed", error=str(error))
        try:
            dynamo.put_event(account_id, tx_timestamp, transaction_id, "ALERT_FAILED",
                             "The alert email could not be sent; it will be retried")
        except dynamo.DynamoError:
            pass                                  # the send failure is what matters; it is raised below
        raise AlertError(f"Could not send the alert for {transaction_id}") from error

    dynamo.mark_alert_sent(account_id, tx_timestamp, transaction_id, message_id)
    try:
        dynamo.put_event(account_id, tx_timestamp, transaction_id, "ALERT_SENT", "Email sent to the compliance team")
    except dynamo.DynamoError:
        log.error("journey_write_failed", stage="ALERT_SENT")     # the email went and is marked; audit trail only
    log.info("alert_sent", ses_message_id=message_id, risk_score=int(score["risk_score"]))
    return "sent"


def handler(event, context):
    """Lambda entry point. `event` holds a batch of DynamoDB stream records."""
    log = logger.bind(request_id=getattr(context, "aws_request_id", "local"))
    counts = {"sent": 0, "duplicate": 0, "ignored": 0}
    failures = 0

    for record in event.get("Records", []):
        try:
            counts[process_record(record, log)] += 1
        except (AlertError, dynamo.DynamoError, KeyError) as error:
            failures += 1
            log.error("alert_record_failed", error_type=type(error).__name__, error=str(error))

    log.info("batch_finished", received=len(event.get("Records", [])), failed=failures, **counts)
    if failures:
        # Raising makes the stream retry the batch. Alerts already sent are
        # skipped on the retry because of their markers.
        raise AlertError(f"{failures} alert(s) could not be processed")
    return counts
