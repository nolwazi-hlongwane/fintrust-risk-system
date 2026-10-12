"""Integration tests: the score Lambda against a real PostgreSQL ledger
(as the fintrust_score database user) and a moto DynamoDB table."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fintrust import db, dynamo, score
from fintrust.models import Transaction

pytestmark = pytest.mark.integration

SAST = timezone(timedelta(hours=2))
BASE = datetime(2026, 11, 1, 9, 0, tzinfo=SAST)   # after the seed data, so velocity counts start from zero


class Context:
    aws_request_id = "test-request-id"


@pytest.fixture
def ledger(as_db_user, events_table, admin):
    as_db_user("fintrust_score")
    return admin


def make(number, account="ACC-1001", amount="100.00", country="ZA", minutes=0):
    return Transaction(
        transaction_id=f"TXN-TEST-{number:06d}",
        account_id=account,
        tx_date=BASE + timedelta(minutes=minutes),
        amount=Decimal(amount),
        tx_type="DEBIT",
        channel="EFT",
        country_code=country,
        merchant_name="Test Merchant",
        description="Integration test",
        source_file="tests/integration",
    )


def record(transaction, receive_count=1):
    return {
        "messageId": f"msg-{transaction.transaction_id}",
        "body": json.dumps(transaction.to_message()),
        "attributes": {"MessageGroupId": transaction.account_id, "ApproximateReceiveCount": str(receive_count)},
    }


def run(*transactions, receive_count=1):
    return score.handler({"Records": [record(t, receive_count) for t in transactions]}, Context())


def rows(admin, sql, params=None):
    with admin.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


class TestLedgerWrites:
    def test_low_risk_transaction_is_stored_with_no_events_or_alert(self, ledger):
        assert run(make(1)) == {"batchItemFailures": []}
        stored = rows(ledger, "SELECT amount, risk_score, risk_level, merchant_name, source_file "
                              "FROM transactions WHERE transaction_id = 'TXN-TEST-000001'")
        assert stored == [(Decimal("100.00"), 0, "LOW", "Test Merchant", "tests/integration")]
        assert rows(ledger, "SELECT 1 FROM risk_events WHERE transaction_id = 'TXN-TEST-000001'") == []
        assert rows(ledger, "SELECT 1 FROM alerts WHERE transaction_id = 'TXN-TEST-000001'") == []

    def test_high_risk_transaction_gets_rule_events_and_an_open_alert(self, ledger):
        run(make(2, account="ACC-1007", amount="88750.00", country="AE"))
        assert rows(ledger, "SELECT risk_score, risk_level FROM transactions "
                            "WHERE transaction_id = 'TXN-TEST-000002'") == [(70, "HIGH")]
        events = rows(ledger, "SELECT event_type, points, reason, account_id FROM risk_events "
                              "WHERE transaction_id = 'TXN-TEST-000002' ORDER BY risk_event_id")
        assert events == [
            ("HIGH_AMOUNT", 50, "Amount 88750.00 is above 50000", "ACC-1007"),
            ("CROSS_BORDER", 20, "Country AE is not ZA", "ACC-1007"),
        ]
        assert rows(ledger, "SELECT status FROM alerts WHERE transaction_id = 'TXN-TEST-000002'") == [("OPEN",)]

    def test_medium_risk_transaction_gets_events_but_no_alert(self, ledger):
        run(make(3, amount="75000.00"))
        assert rows(ledger, "SELECT risk_level FROM transactions WHERE transaction_id = 'TXN-TEST-000003'") == [("MEDIUM",)]
        assert rows(ledger, "SELECT 1 FROM alerts WHERE transaction_id = 'TXN-TEST-000003'") == []

    def test_timestamp_is_stored_exactly(self, ledger):
        run(make(4, minutes=17))
        stored = rows(ledger, "SELECT tx_date FROM transactions WHERE transaction_id = 'TXN-TEST-000004'")[0][0]
        assert stored == BASE + timedelta(minutes=17)

    def test_score_in_dynamodb_matches_the_ledger(self, ledger):
        run(make(5, account="ACC-1007", amount="88750.00", country="AE"))
        journey = dynamo.get_journey("TXN-TEST-000005")
        assert (journey["score"]["risk_score"], journey["score"]["risk_level"]) == (70, "HIGH")
        assert [rule["rule"] for rule in journey["score"]["rules"]] == ["HIGH_AMOUNT", "CROSS_BORDER"]
        assert journey["events"][-1]["stage"] == "SCORED"


class TestVelocity:
    def test_eleventh_transaction_within_an_hour_fires_the_rule(self, ledger):
        batch = [make(100 + n, account="ACC-1003", minutes=n * 5) for n in range(12)]   # 0, 5, ... 55 minutes
        assert run(*batch) == {"batchItemFailures": []}
        scores = dict(rows(ledger, "SELECT transaction_id, risk_score FROM transactions "
                                   "WHERE transaction_id LIKE 'TXN-TEST-0001%' ORDER BY transaction_id"))
        assert [scores[f"TXN-TEST-{100 + n:06d}"] for n in range(12)] == [0] * 10 + [30, 30]
        reason = rows(ledger, "SELECT reason FROM risk_events WHERE transaction_id = 'TXN-TEST-000110'")[0][0]
        assert reason == "11 transactions on the account within 60 minutes"

    def test_transactions_more_than_an_hour_apart_do_not_count(self, ledger):
        run(*[make(200 + n, account="ACC-1004", minutes=n * 7) for n in range(12)])    # spread over 77 minutes
        highest = rows(ledger, "SELECT max(risk_score) FROM transactions WHERE transaction_id LIKE 'TXN-TEST-0002%'")
        assert highest == [(0,)]

    def test_other_accounts_do_not_count(self, ledger):
        run(*[make(300 + n, account="ACC-1005", minutes=n) for n in range(10)])
        run(make(310, account="ACC-1006", minutes=10))
        assert rows(ledger, "SELECT risk_score FROM transactions WHERE transaction_id = 'TXN-TEST-000310'") == [(0,)]

    def test_all_three_rules_together_score_100(self, ledger):
        run(*[make(400 + n, account="ACC-1008", minutes=n) for n in range(10)])
        run(make(410, account="ACC-1008", amount="60000.00", country="GB", minutes=10))
        assert rows(ledger, "SELECT risk_score, risk_level FROM transactions "
                            "WHERE transaction_id = 'TXN-TEST-000410'") == [(100, "HIGH")]


class TestFailureAndRecovery:
    def test_unknown_account_fails_and_writes_nothing_to_the_ledger(self, ledger):
        poison = make(500, account="ACC-9999")
        assert run(poison) == {"batchItemFailures": [{"itemIdentifier": "msg-TXN-TEST-000500"}]}
        assert rows(ledger, "SELECT 1 FROM transactions WHERE transaction_id = 'TXN-TEST-000500'") == []
        event = dynamo.get_journey("TXN-TEST-000500")["events"][0]
        assert event["stage"] == "SCORE_FAILED"
        assert event["detail"] == "Attempt 1 of 3: Account ACC-**99 is not in the ledger (not onboarded yet)"

    def test_break_and_recover(self, ledger):
        """The live demo, as a test: a transaction for an account that does not
        exist yet fails three times, then succeeds once the account is onboarded
        and the message is delivered again (the redrive)."""
        poison = make(600, account="ACC-9998", amount="72000.00", country="GB")
        try:
            for attempt in (1, 2, 3):
                assert len(run(poison, receive_count=attempt)["batchItemFailures"]) == 1
            assert rows(ledger, "SELECT 1 FROM transactions WHERE transaction_id = 'TXN-TEST-000600'") == []

            with ledger.cursor() as cursor:   # the fix: onboard the account
                cursor.execute("INSERT INTO accounts (account_id, customer_id, account_type, opened_date) "
                               "VALUES ('ACC-9998', 'CUST-0001', 'CHEQUE', '2026-11-01')")

            assert run(poison) == {"batchItemFailures": []}   # the redriven message
            assert rows(ledger, "SELECT risk_score, risk_level FROM transactions "
                                "WHERE transaction_id = 'TXN-TEST-000600'") == [(70, "HIGH")]
            journey = dynamo.get_journey("TXN-TEST-000600")
            assert [e["stage"] for e in journey["events"]] == ["SCORE_FAILED"] * 3 + ["SCORED"]
            assert journey["score"]["risk_level"] == "HIGH"
        finally:
            with ledger.cursor() as cursor:
                for table in ("alerts", "risk_events", "transactions"):
                    cursor.execute(f"DELETE FROM {table} WHERE transaction_id = 'TXN-TEST-000600'")
                cursor.execute("DELETE FROM accounts WHERE account_id = 'ACC-9998'")

    def test_one_bad_account_does_not_stop_the_others(self, ledger):
        good_before, poison, held_back, good_other = (
            make(700, "ACC-1001"), make(701, "ACC-9999"), make(702, "ACC-9999", minutes=1), make(703, "ACC-1003"))
        result = run(good_before, poison, held_back, good_other)
        assert [item["itemIdentifier"] for item in result["batchItemFailures"]] == [
            "msg-TXN-TEST-000701", "msg-TXN-TEST-000702"]
        stored = [row[0] for row in rows(ledger, "SELECT transaction_id FROM transactions "
                                                 "WHERE transaction_id LIKE 'TXN-TEST-0007%' ORDER BY 1")]
        assert stored == ["TXN-TEST-000700", "TXN-TEST-000703"]

    def test_same_message_delivered_twice_creates_no_duplicates(self, ledger):
        transaction = make(800, account="ACC-1007", amount="88750.00", country="AE")
        run(transaction)
        assert run(transaction, receive_count=2) == {"batchItemFailures": []}
        assert rows(ledger, "SELECT count(*) FROM transactions WHERE transaction_id = 'TXN-TEST-000800'") == [(1,)]
        assert rows(ledger, "SELECT count(*) FROM risk_events WHERE transaction_id = 'TXN-TEST-000800'") == [(2,)]
        assert rows(ledger, "SELECT count(*) FROM alerts WHERE transaction_id = 'TXN-TEST-000800'") == [(1,)]
        events = dynamo.get_journey("TXN-TEST-000800")["events"]
        assert events[-1]["detail"] == "Score 70 (HIGH); already in the ledger"

    def test_redelivery_restores_a_score_that_never_reached_dynamodb(self, ledger):
        """RDS committed but the DynamoDB write failed: the retry must write the
        stored score, not score again."""
        from unittest.mock import patch

        transaction = make(801, account="ACC-1007", amount="88750.00", country="AE")
        with patch.object(dynamo, "put_score", side_effect=dynamo.DynamoError("throttled")):
            assert len(run(transaction)["batchItemFailures"]) == 1
        assert rows(ledger, "SELECT count(*) FROM transactions WHERE transaction_id = 'TXN-TEST-000801'") == [(1,)]
        assert dynamo.get_journey("TXN-TEST-000801")["score"] is None

        assert run(transaction, receive_count=2) == {"batchItemFailures": []}
        restored = dynamo.get_journey("TXN-TEST-000801")["score"]
        assert restored["risk_score"] == 70
        assert [rule["rule"] for rule in restored["rules"]] == ["HIGH_AMOUNT", "CROSS_BORDER"]
        assert rows(ledger, "SELECT count(*) FROM risk_events WHERE transaction_id = 'TXN-TEST-000801'") == [(2,)]

    def test_failure_part_way_rolls_back_the_whole_transaction(self, ledger, monkeypatch):
        """The transaction row inserts, then the alert insert fails: nothing may remain."""
        monkeypatch.setattr(score, "INSERT_ALERT", "INSERT INTO alerts (transaction_id, status) VALUES (%s, 'BROKEN')")
        transaction = make(900, account="ACC-1007", amount="88750.00", country="AE")
        assert len(run(transaction)["batchItemFailures"]) == 1
        assert rows(ledger, "SELECT 1 FROM transactions WHERE transaction_id = 'TXN-TEST-000900'") == []
        assert rows(ledger, "SELECT 1 FROM risk_events WHERE transaction_id = 'TXN-TEST-000900'") == []
        detail = dynamo.get_journey("TXN-TEST-000900")["events"][0]["detail"]
        assert detail == "Attempt 1 of 3: The database rejected the transaction"

    def test_database_unreachable_fails_the_message(self, ledger, monkeypatch):
        db.close_connection()
        monkeypatch.setenv("FINTRUST_DB_PORT", "1")
        assert len(run(make(901))["batchItemFailures"]) == 1
        detail = dynamo.get_journey("TXN-TEST-000901")["events"][0]["detail"]
        assert detail == "Attempt 1 of 3: The database could not be reached"

    def test_one_connection_serves_the_whole_batch(self, ledger):
        run(make(950), make(951, "ACC-1003"), make(952, "ACC-1004"))
        first = db.get_connection()
        run(make(953))
        assert db.get_connection() is first


def test_seed_data_scores_agree_with_the_live_scorer(as_db_user, events_table, admin):
    """Re-score a sample of seed transactions with the Lambda's own rules and
    compare with what seed.sql says. Proves the seed generator and the scorer
    use the same rules."""
    as_db_user("fintrust_score")
    from fintrust import rules

    with admin.cursor() as cursor:
        cursor.execute("""
            SELECT t.transaction_id, t.account_id, t.tx_date, t.amount, t.country_code, t.risk_score,
                   (SELECT count(*) FROM transactions o WHERE o.account_id = t.account_id
                      AND o.tx_date > t.tx_date - interval '60 minutes' AND o.tx_date <= t.tx_date) AS in_window
            FROM transactions t WHERE t.transaction_id NOT LIKE 'TXN-TEST-%'
        """)
        seed = cursor.fetchall()
    assert len(seed) >= 200
    for transaction_id, account_id, tx_date, amount, country, expected, in_window in seed:
        transaction = Transaction(transaction_id, account_id, tx_date, amount, "DEBIT", "EFT", country, "seed")
        assert rules.total_points(rules.all_hits(transaction, in_window)) == expected, transaction_id
