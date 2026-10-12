"""Unit tests for fintrust.dynamo, against a moto fake table."""

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from fintrust import dynamo
from fintrust.dynamo import DynamoError
from fintrust.models import RiskScore, RuleHit

ACCOUNT = "ACC-1007"
STAMP = "2026-09-15T13:10:00Z"
TXN = "TXN-20260915-000231"


def make_score(transaction_id=TXN, tx_timestamp=STAMP, account_id=ACCOUNT, score=70, level="HIGH"):
    return RiskScore(
        transaction_id=transaction_id,
        account_id=account_id,
        tx_timestamp=tx_timestamp,
        risk_score=score,
        risk_level=level,
        rules=(
            RuleHit("HIGH_AMOUNT", 50, "Amount 104500.00 is above 50000"),
            RuleHit("CROSS_BORDER", 20, "Country AE is not ZA"),
        ) if score else (),
        scored_at=datetime(2026, 9, 15, 13, 10, 4, tzinfo=timezone.utc),
    )


class TestPutAndGetScore:
    def test_score_round_trip(self, events_table):
        dynamo.put_score(make_score())
        item = dynamo.get_score(ACCOUNT, STAMP, TXN)
        assert item["risk_score"] == 70
        assert item["risk_level"] == "HIGH"
        assert item["record_type"] == "SCORE"
        assert item["transaction_id"] == TXN

    def test_rule_breakdown_is_stored(self, events_table):
        dynamo.put_score(make_score())
        rules = dynamo.get_score(ACCOUNT, STAMP, TXN)["rules"]
        assert rules == [
            {"rule": "HIGH_AMOUNT", "points": 50, "detail": "Amount 104500.00 is above 50000"},
            {"rule": "CROSS_BORDER", "points": 20, "detail": "Country AE is not ZA"},
        ]

    def test_numbers_come_back_as_int_not_decimal(self, events_table):
        dynamo.put_score(make_score())
        item = dynamo.get_score(ACCOUNT, STAMP, TXN)
        assert type(item["risk_score"]) is int
        assert type(item["rules"][0]["points"]) is int

    def test_sort_key_starts_with_timestamp_and_includes_transaction(self, events_table):
        item = dynamo.put_score(make_score())
        assert item["tx_timestamp"] == f"{STAMP}#{TXN}#SCORE"

    def test_same_second_transactions_do_not_overwrite_each_other(self, events_table):
        dynamo.put_score(make_score(transaction_id="TXN-A"))
        dynamo.put_score(make_score(transaction_id="TXN-B", score=0, level="LOW"))
        assert dynamo.get_score(ACCOUNT, STAMP, "TXN-A")["risk_level"] == "HIGH"
        assert dynamo.get_score(ACCOUNT, STAMP, "TXN-B")["risk_level"] == "LOW"

    def test_writing_twice_is_harmless(self, events_table):
        dynamo.put_score(make_score())
        dynamo.put_score(make_score())
        assert len(dynamo.query_by_account(ACCOUNT)) == 1

    def test_missing_score_returns_none(self, events_table):
        assert dynamo.get_score(ACCOUNT, STAMP, "TXN-NOPE") is None

    def test_item_expires_in_about_a_year(self, events_table):
        item = dynamo.put_score(make_score())
        year = 365 * 24 * 60 * 60
        assert abs(item["expires_at"] - (time.time() + year)) < 60


class TestEvents:
    def test_event_is_stored_with_stage_and_time(self, events_table):
        item = dynamo.put_event(ACCOUNT, STAMP, TXN, "QUEUED")
        assert item["stage"] == "QUEUED"
        assert item["record_type"] == "EVENT"
        assert item["at"].endswith("Z")
        assert "detail" not in item

    def test_event_detail_is_stored_and_truncated(self, events_table):
        item = dynamo.put_event(ACCOUNT, STAMP, TXN, "SCORE_FAILED", detail="x" * 900)
        assert len(item["detail"]) == 500

    def test_unknown_stage_is_rejected(self, events_table):
        with pytest.raises(ValueError, match="Unknown stage"):
            dynamo.put_event(ACCOUNT, STAMP, TXN, "TELEPORTED")

    def test_repeated_failures_are_all_kept(self, events_table):
        for attempt in (1, 2, 3):
            dynamo.put_event(ACCOUNT, STAMP, TXN, "SCORE_FAILED", detail=f"attempt {attempt}")
            time.sleep(0.002)
        events = dynamo.get_journey(TXN)["events"]
        assert [event["detail"] for event in events] == ["attempt 1", "attempt 2", "attempt 3"]


class TestJourney:
    def test_journey_has_score_and_events_in_order(self, events_table):
        for stage in ("RECEIVED", "VALIDATED", "QUEUED"):
            dynamo.put_event(ACCOUNT, STAMP, TXN, stage)
            time.sleep(0.002)
        dynamo.put_score(make_score())
        dynamo.put_event(ACCOUNT, STAMP, TXN, "SCORED")
        journey = dynamo.get_journey(TXN)
        assert journey["score"]["risk_score"] == 70
        assert [event["stage"] for event in journey["events"]] == [
            "RECEIVED", "VALIDATED", "QUEUED", "SCORED",
        ]

    def test_journey_of_a_transaction_that_was_never_scored(self, events_table):
        dynamo.put_event(ACCOUNT, STAMP, TXN, "QUEUED")
        dynamo.put_event(ACCOUNT, STAMP, TXN, "SCORE_FAILED", detail="account not found")
        journey = dynamo.get_journey(TXN)
        assert journey["score"] is None
        assert len(journey["events"]) == 2

    def test_journey_only_contains_that_transaction(self, events_table):
        dynamo.put_event(ACCOUNT, STAMP, TXN, "QUEUED")
        dynamo.put_event(ACCOUNT, STAMP, "TXN-OTHER", "QUEUED")
        assert len(dynamo.get_journey(TXN)["events"]) == 1

    def test_unknown_transaction_gives_empty_journey(self, events_table):
        assert dynamo.get_journey("TXN-NOPE") == {"score": None, "events": []}


class TestQueryByAccount:
    def load(self):
        dynamo.put_score(make_score("TXN-1", "2026-09-01T08:00:00Z", score=0, level="LOW"))
        dynamo.put_score(make_score("TXN-2", "2026-09-10T08:00:00Z"))
        dynamo.put_score(make_score("TXN-3", "2026-09-20T08:00:00Z", score=0, level="LOW"))
        dynamo.put_score(make_score("TXN-9", "2026-09-15T08:00:00Z", account_id="ACC-2000"))
        dynamo.put_event(ACCOUNT, "2026-09-10T08:00:00Z", "TXN-2", "SCORED")

    def test_newest_first_by_default(self, events_table):
        self.load()
        ids = [i["transaction_id"] for i in dynamo.query_by_account(ACCOUNT, scores_only=True)]
        assert ids == ["TXN-3", "TXN-2", "TXN-1"]

    def test_oldest_first(self, events_table):
        self.load()
        items = dynamo.query_by_account(ACCOUNT, newest_first=False, scores_only=True)
        assert [i["transaction_id"] for i in items] == ["TXN-1", "TXN-2", "TXN-3"]

    def test_only_that_account(self, events_table):
        self.load()
        assert {i["account_id"] for i in dynamo.query_by_account(ACCOUNT)} == {ACCOUNT}

    def test_since_filters_by_transaction_time(self, events_table):
        self.load()
        items = dynamo.query_by_account(ACCOUNT, since="2026-09-10T00:00:00Z", scores_only=True)
        assert [i["transaction_id"] for i in items] == ["TXN-3", "TXN-2"]

    def test_limit(self, events_table):
        self.load()
        assert len(dynamo.query_by_account(ACCOUNT, limit=2, scores_only=True)) == 2

    def test_events_are_included_unless_scores_only(self, events_table):
        self.load()
        assert len(dynamo.query_by_account(ACCOUNT)) == 4
        assert len(dynamo.query_by_account(ACCOUNT, scores_only=True)) == 3

    def test_unknown_account_gives_empty_list(self, events_table):
        assert dynamo.query_by_account("ACC-0000") == []

    def test_reads_every_page(self, events_table):
        for number in range(30):
            dynamo.put_score(make_score(f"TXN-{number:03d}", f"2026-09-01T08:{number:02d}:00Z"))
        pages = {"count": 0}
        real_query = dynamo.table().query

        def small_pages(**arguments):
            pages["count"] += 1
            return real_query(Limit=7, **arguments)

        with patch.object(dynamo.table(), "query", side_effect=small_pages):
            items = dynamo.query_by_account(ACCOUNT, limit=25)
        assert len(items) == 25
        assert pages["count"] == 4


class TestErrors:
    def test_missing_table_raises_dynamo_error_and_logs(self, aws, monkeypatch, capsys):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_DYNAMO_TABLE", "table-that-does-not-exist")
        dynamo.reset()
        with pytest.raises(DynamoError, match="put_score"):
            dynamo.put_score(make_score())
        assert "dynamo_failed" in capsys.readouterr().out
        dynamo.reset()

    def test_throttling_is_wrapped(self, events_table):
        throttled = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
            "Query",
        )
        with patch.object(dynamo.table(), "query", side_effect=throttled):
            with pytest.raises(DynamoError):
                dynamo.query_by_account(ACCOUNT)

    def test_table_name_comes_from_config(self, events_table):
        assert dynamo.table().name == "fintrust-capstone-events"

    def test_resource_is_created_at_module_level(self):
        assert dynamo._dynamodb.meta.service_name == "dynamodb"


def test_events_written_in_the_same_millisecond_are_all_kept(events_table):
    """Regression: a fast machine wrote two SCORE_FAILED events in one millisecond
    and the second replaced the first, because the sort key only had milliseconds."""
    for attempt in range(1, 51):
        dynamo.put_event("ACC-1001", "2026-09-15T13:10:00Z", "TXN-20260915-000900", "SCORE_FAILED", f"Attempt {attempt}")
    events = dynamo.get_journey("TXN-20260915-000900")["events"]
    assert [event["detail"] for event in events] == [f"Attempt {attempt}" for attempt in range(1, 51)]
