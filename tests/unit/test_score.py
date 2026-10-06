"""Unit tests for the score Lambda's batch handling, with the database stubbed.

The scoring SQL itself is tested against a real PostgreSQL in
tests/integration/test_score_integration.py.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from fintrust import db, dynamo, score
from fintrust.models import RiskScore, RuleHit, Transaction

SAST = timezone(timedelta(hours=2))


class Context:
    aws_request_id = "test-request-id"


def make_transaction(number=1, account="ACC-1001", amount="100.00", country="ZA"):
    return Transaction(
        transaction_id=f"TXN-TEST-{number:06d}",
        account_id=account,
        tx_date=datetime(2026, 11, 1, 9, number % 60, tzinfo=SAST),
        amount=Decimal(amount),
        tx_type="DEBIT",
        channel="EFT",
        country_code=country,
        source_file="tests",
    )


def record(transaction, receive_count=1, message_id=None, body=None):
    return {
        "messageId": message_id or f"msg-{transaction.transaction_id}",
        "body": body if body is not None else json.dumps(transaction.to_message()),
        "attributes": {"MessageGroupId": transaction.account_id, "ApproximateReceiveCount": str(receive_count)},
    }


def fake_score(transaction, level="LOW", points=0):
    hits = (RuleHit("HIGH_AMOUNT", 50, "x"), RuleHit("CROSS_BORDER", 20, "y")) if points else ()
    return RiskScore(transaction.transaction_id, transaction.account_id, transaction.tx_timestamp, points, level, hits)


@pytest.fixture
def stub_scoring(events_table):
    """Replace the database step: every transaction scores LOW unless told otherwise."""
    outcomes = {}

    def scorer(transaction):
        outcome = outcomes.get(transaction.transaction_id)
        if isinstance(outcome, Exception):
            raise outcome
        return (outcome or fake_score(transaction)), True

    with patch.object(score, "score_transaction", side_effect=scorer):
        yield outcomes


def failed_ids(result):
    return [item["itemIdentifier"] for item in result["batchItemFailures"]]


class TestSuccess:
    def test_all_messages_succeed(self, stub_scoring):
        transactions = [make_transaction(1), make_transaction(2, "ACC-1003")]
        result = score.handler({"Records": [record(t) for t in transactions]}, Context())
        assert result == {"batchItemFailures": []}

    def test_score_and_scored_step_are_written_to_dynamodb(self, stub_scoring):
        transaction = make_transaction(1)
        stub_scoring[transaction.transaction_id] = fake_score(transaction, "HIGH", 70)
        score.handler({"Records": [record(transaction)]}, Context())
        journey = dynamo.get_journey(transaction.transaction_id)
        assert journey["score"]["risk_score"] == 70
        assert [rule["rule"] for rule in journey["score"]["rules"]] == ["HIGH_AMOUNT", "CROSS_BORDER"]
        assert journey["events"][-1]["stage"] == "SCORED"
        assert journey["events"][-1]["detail"] == "Score 70 (HIGH)"

    def test_empty_event(self, stub_scoring):
        assert score.handler({}, Context()) == {"batchItemFailures": []}

    def test_scoring_is_logged_without_personal_details(self, stub_scoring, capsys):
        score.handler({"Records": [record(make_transaction(1))]}, Context())
        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        scored = next(line for line in lines if line["message"] == "transaction_scored")
        assert scored["transaction_id"] == "TXN-TEST-000001"
        assert scored["request_id"] == "test-request-id"
        assert "ACC-1001" not in json.dumps(lines)


class TestFailure:
    def test_failed_message_is_reported_and_others_succeed(self, stub_scoring):
        good, bad = make_transaction(1, "ACC-1001"), make_transaction(2, "ACC-9999")
        stub_scoring[bad.transaction_id] = score.AccountNotFound("Account ACC-**99 is not in the ledger")
        result = score.handler({"Records": [record(bad), record(good)]}, Context())
        assert failed_ids(result) == ["msg-TXN-TEST-000002"]
        assert dynamo.get_journey(good.transaction_id)["score"] is not None

    def test_later_messages_for_the_same_account_are_held_back_in_order(self, stub_scoring):
        first, second, third = (make_transaction(n, "ACC-1001") for n in (1, 2, 3))
        other = make_transaction(4, "ACC-1003")
        stub_scoring[second.transaction_id] = db.DatabaseError("boom")
        result = score.handler({"Records": [record(first), record(second), record(third), record(other)]}, Context())
        assert failed_ids(result) == ["msg-TXN-TEST-000002", "msg-TXN-TEST-000003"]
        assert dynamo.get_journey(first.transaction_id)["score"] is not None
        assert dynamo.get_journey(third.transaction_id) == {"score": None, "events": []}   # never attempted
        assert dynamo.get_journey(other.transaction_id)["score"] is not None                # other account unaffected

    def test_failed_attempt_is_recorded_in_the_journey(self, stub_scoring):
        bad = make_transaction(2, "ACC-9999")
        stub_scoring[bad.transaction_id] = score.AccountNotFound(
            "Account ACC-**99 is not in the ledger (not onboarded yet)")
        score.handler({"Records": [record(bad, receive_count=1)]}, Context())
        event = dynamo.get_journey(bad.transaction_id)["events"][0]
        assert event["stage"] == "SCORE_FAILED"
        assert event["detail"] == "Attempt 1 of 3: Account ACC-**99 is not in the ledger (not onboarded yet)"

    def test_last_attempt_says_it_moves_to_the_dead_letter_queue(self, stub_scoring):
        bad = make_transaction(2, "ACC-9999")
        stub_scoring[bad.transaction_id] = score.AccountNotFound("Account ACC-**99 is not in the ledger")
        for attempt in (1, 2, 3):
            score.handler({"Records": [record(bad, receive_count=attempt)]}, Context())
        details = [event["detail"] for event in dynamo.get_journey(bad.transaction_id)["events"]]
        assert len(details) == 3
        assert "dead-letter queue" not in details[1]
        assert details[2].startswith("Attempt 3 of 3") and details[2].endswith("Moved to the dead-letter queue; nothing is lost.")

    @pytest.mark.parametrize(
        "error, expected",
        [
            (db.DatabaseUnavailable("could not connect to server at 10.20.1.5"), "The database could not be reached"),
            (db.DatabaseError('insert violates check constraint "ck_x"'), "The database rejected the transaction"),
            (RuntimeError("secret internal detail"), "Scoring failed (RuntimeError)"),
        ],
    )
    def test_journey_reason_never_exposes_internal_detail(self, stub_scoring, error, expected):
        bad = make_transaction(2)
        stub_scoring[bad.transaction_id] = error
        score.handler({"Records": [record(bad)]}, Context())
        assert dynamo.get_journey(bad.transaction_id)["events"][0]["detail"] == f"Attempt 1 of 3: {expected}"

    def test_database_down_fails_every_message(self, stub_scoring):
        transactions = [make_transaction(1, "ACC-1001"), make_transaction(2, "ACC-1003")]
        for transaction in transactions:
            stub_scoring[transaction.transaction_id] = db.DatabaseUnavailable("down")
        result = score.handler({"Records": [record(t) for t in transactions]}, Context())
        assert len(failed_ids(result)) == 2

    @pytest.mark.parametrize("body", ["not json", "{}", '{"transaction_id": "T"}', "[1, 2]", "null"])
    def test_malformed_message_is_reported_as_failed(self, stub_scoring, body):
        bad = record(make_transaction(1), body=body)
        assert failed_ids(score.handler({"Records": [bad]}, Context())) == ["msg-TXN-TEST-000001"]

    def test_message_with_bad_amount_is_reported_as_failed(self, stub_scoring):
        message = make_transaction(1).to_message()
        message["amount"] = "lots"
        result = score.handler({"Records": [record(make_transaction(1), body=json.dumps(message))]}, Context())
        assert failed_ids(result) == ["msg-TXN-TEST-000001"]

    def test_score_store_failure_fails_the_message_so_it_is_retried(self, stub_scoring):
        transaction = make_transaction(1)
        with patch.object(dynamo, "put_score", side_effect=dynamo.DynamoError("throttled")):
            result = score.handler({"Records": [record(transaction)]}, Context())
        assert failed_ids(result) == ["msg-TXN-TEST-000001"]

    def test_journey_store_failure_alone_does_not_fail_the_message(self, stub_scoring, capsys):
        transaction = make_transaction(1)
        with patch.object(dynamo, "put_event", side_effect=dynamo.DynamoError("throttled")):
            result = score.handler({"Records": [record(transaction)]}, Context())
        assert result == {"batchItemFailures": []}
        assert "journey_write_failed" in capsys.readouterr().out

    def test_failure_is_logged_with_its_detail(self, stub_scoring, capsys):
        bad = make_transaction(2)
        stub_scoring[bad.transaction_id] = RuntimeError("the real reason")
        score.handler({"Records": [record(bad, receive_count=2)]}, Context())
        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        failure = next(line for line in lines if line["message"] == "scoring_failed")
        assert failure["level"] == "ERROR"
        assert failure["error"] == "the real reason" and failure["attempt"] == 2
        finished = next(line for line in lines if line["message"] == "batch_finished")
        assert (finished["received"], finished["scored"], finished["failed"]) == (1, 0, 1)
