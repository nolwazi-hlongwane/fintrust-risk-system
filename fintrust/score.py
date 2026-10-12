"""Score Lambda: triggered by the SQS FIFO queue, one message per transaction.

For each message:

    1. rebuild the Transaction from the message
    2. inside ONE database transaction:
         - check the account exists
         - count the account's transactions in the last hour (velocity rule)
         - apply every risk rule (fintrust/rules.py)
         - insert the transaction, one risk_events row per rule that fired,
           and an alert if the score is HIGH
       either all of that is committed, or none of it is
    3. write the score, with its rule breakdown, to DynamoDB
    4. record the SCORED step of the journey

This Lambda runs INSIDE the VPC because it needs the database. It reaches
DynamoDB through the free gateway endpoint. It never calls SQS itself: the
Lambda service reads the queue and hands the messages to this function.

How failure is handled (this is what makes the dead-letter queue work):
    * If scoring a message fails, its id is returned in "batchItemFailures".
      Lambda then leaves that message on the queue; SQS shows it again after the
      visibility timeout. After 3 failed attempts SQS moves it to the DLQ.
    * Messages that succeeded in the same batch are deleted as normal.
    * FIFO order: once one message for an account fails, later messages for the
      SAME account in the batch are not attempted (they are returned as failed
      too), so an account's transactions are never scored out of order. Other
      accounts in the batch are not affected.

Safe to receive the same message twice: if the transaction is already in the
ledger it is not scored again; its stored result is simply written to DynamoDB
again. A redelivery can therefore never create a duplicate ledger row.
"""

import json
from datetime import timedelta

from fintrust import config, db, dynamo, metrics, rules
from fintrust.log import get_logger
from fintrust.models import RiskScore, RuleHit, Transaction
from fintrust.validators import mask_account

logger = get_logger("score")


class ScoringError(Exception):
    """This message cannot be scored right now. It stays on the queue for a retry."""


class AccountNotFound(ScoringError):
    """The transaction's account is not in the ledger (not onboarded yet)."""


INSERT_TRANSACTION = """
    INSERT INTO transactions (transaction_id, account_id, tx_date, amount, currency, tx_type, channel,
                              merchant_name, country_code, description, risk_score, risk_level, source_file)
    VALUES (%(transaction_id)s, %(account_id)s, %(tx_date)s, %(amount)s, %(currency)s, %(tx_type)s, %(channel)s,
            %(merchant_name)s, %(country_code)s, %(description)s, %(risk_score)s, %(risk_level)s, %(source_file)s)
"""

INSERT_RISK_EVENT = """
    INSERT INTO risk_events (account_id, transaction_id, event_type, points, reason)
    VALUES (%s, %s, %s, %s, %s)
"""

INSERT_ALERT = "INSERT INTO alerts (transaction_id) VALUES (%s)"

# The velocity window ends at this transaction and reaches back 60 minutes.
COUNT_RECENT = """
    SELECT count(*) AS recent
    FROM transactions
    WHERE account_id = %s AND tx_date > %s AND tx_date <= %s
"""


def _stored_score(cursor, transaction, row):
    """Rebuild the RiskScore of a transaction that is already in the ledger."""
    cursor.execute(
        "SELECT event_type, points, reason FROM risk_events WHERE transaction_id = %s ORDER BY risk_event_id",
        (transaction.transaction_id,),
    )
    hits = tuple(RuleHit(event["event_type"], event["points"], event["reason"]) for event in cursor.fetchall())
    return RiskScore(
        transaction_id=transaction.transaction_id,
        account_id=transaction.account_id,
        tx_timestamp=transaction.tx_timestamp,
        risk_score=row["risk_score"],
        risk_level=row["risk_level"],
        rules=hits,
    )


def score_transaction(transaction):
    """Score one transaction and store it in the ledger, all or nothing.

    Returns (RiskScore, created). `created` is False when the transaction was
    already in the ledger (the message was delivered a second time).
    """
    with db.transaction() as cursor:
        cursor.execute(
            "SELECT risk_score, risk_level FROM transactions WHERE transaction_id = %s",
            (transaction.transaction_id,),
        )
        existing = cursor.fetchone()
        if existing:
            return _stored_score(cursor, transaction, existing), False

        cursor.execute("SELECT status FROM accounts WHERE account_id = %s", (transaction.account_id,))
        if cursor.fetchone() is None:
            raise AccountNotFound(
                f"Account {mask_account(transaction.account_id)} is not in the ledger (not onboarded yet)"
            )

        window = timedelta(minutes=config.get_int("velocity_window_minutes"))
        cursor.execute(COUNT_RECENT, (transaction.account_id, transaction.tx_date - window, transaction.tx_date))
        in_window = cursor.fetchone()["recent"] + 1          # + 1 for this transaction itself

        score = rules.build_score(transaction, rules.all_hits(transaction, in_window))

        row = transaction.to_message()
        row.update(tx_date=transaction.tx_date, amount=transaction.amount,
                   risk_score=score.risk_score, risk_level=score.risk_level)
        cursor.execute(INSERT_TRANSACTION, row)
        for hit in score.rules:
            cursor.execute(
                INSERT_RISK_EVENT,
                (transaction.account_id, transaction.transaction_id, hit.rule, hit.points, hit.detail),
            )
        if score.is_high_risk:
            cursor.execute(INSERT_ALERT, (transaction.transaction_id,))
    return score, True


def _parse(record):
    try:
        return Transaction.from_message(json.loads(record["body"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise ScoringError(f"Message is not a valid transaction: {type(error).__name__}") from error


def _reason_for_journey(error):
    """A short, safe explanation for the dashboard. Full detail goes to the logs."""
    if isinstance(error, AccountNotFound):
        return str(error)
    if isinstance(error, db.DatabaseUnavailable):
        return "The database could not be reached"
    if isinstance(error, db.DatabaseError):
        return "The database rejected the transaction"
    if isinstance(error, dynamo.DynamoError):
        return "The score could not be written to the event store"
    return f"Scoring failed ({type(error).__name__})"


def _record_failure(transaction, attempt, error, log):
    """Best effort: note the failed attempt in the transaction's journey."""
    limit = config.get_int("max_receive_count")
    detail = f"Attempt {attempt} of {limit}: {_reason_for_journey(error)}"
    if attempt >= limit:
        detail += ". Moved to the dead-letter queue; nothing is lost."
    try:
        dynamo.put_event(
            transaction.account_id, transaction.tx_timestamp, transaction.transaction_id, "SCORE_FAILED", detail
        )
    except dynamo.DynamoError:
        log.error("journey_write_failed", stage="SCORE_FAILED")


def process_record(record, log):
    """Score one SQS message. Raises on any failure so the message is retried."""
    attempt = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
    transaction = _parse(record)
    log = log.bind(transaction_id=transaction.transaction_id, attempt=attempt)

    try:
        score, created = score_transaction(transaction)
        # The score in DynamoDB is what triggers the alert email, so it must be
        # written. If it fails, the message is retried and takes the
        # "already in the ledger" path above, which writes it again.
        dynamo.put_score(score)
    except Exception as error:
        log.error("scoring_failed", error_type=type(error).__name__, error=str(error))
        _record_failure(transaction, attempt, error, log)
        raise

    try:
        dynamo.put_event(
            transaction.account_id, transaction.tx_timestamp, transaction.transaction_id, "SCORED",
            f"Score {score.risk_score} ({score.risk_level})" + ("" if created else "; already in the ledger"),
        )
    except dynamo.DynamoError:
        log.error("journey_write_failed", stage="SCORED")   # audit trail only; the score itself is stored

    log.info(
        "transaction_scored",
        risk_score=score.risk_score,
        risk_level=score.risk_level,
        rules=[hit.rule for hit in score.rules],
        created=created,
    )
    return score, created


def handler(event, context):
    """Lambda entry point. `event` holds a batch of SQS messages."""
    log = logger.bind(request_id=getattr(context, "aws_request_id", "local"))
    failed_ids = []
    blocked_groups = set()      # accounts with a failed message earlier in this batch
    scored = 0
    new_transactions = 0        # written to the ledger for the first time in this batch
    new_high_risk = 0
    failures = 0                # scoring attempts that raised an error

    for record in event.get("Records", []):
        message_id = record["messageId"]
        group = record.get("attributes", {}).get("MessageGroupId")

        if group is not None and group in blocked_groups:
            failed_ids.append(message_id)       # keep this account's messages in order
            continue
        try:
            score, created = process_record(record, log)
            scored += 1
            if created:
                # A redelivered message that is already in the ledger is not counted again.
                new_transactions += 1
                new_high_risk += score.risk_level == "HIGH"
        except Exception:
            # Any failure, expected or not, must leave the message on the queue.
            # It was logged with its detail in process_record.
            failures += 1
            failed_ids.append(message_id)
            if group is not None:
                blocked_groups.add(group)

    log.info("batch_finished", received=len(event.get("Records", [])), scored=scored, failed=len(failed_ids))
    # Messages held back only to keep an account's order are not failures themselves.
    metrics.emit(
        TransactionsScored=new_transactions,
        HighRiskTransactions=new_high_risk,
        ScoringFailures=failures,
    )
    return {"batchItemFailures": [{"itemIdentifier": message_id} for message_id in failed_ids]}
