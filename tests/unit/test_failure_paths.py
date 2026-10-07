"""Unit tests for the rarely taken failure paths.

These are the branches that only run when something goes wrong at an awkward
moment (a connection that will not close, an audit write that fails after the
real work succeeded). They are exactly the branches a happy-path test never
reaches, and where a hidden bug would turn one failure into two.
"""

from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from fintrust import alert, db, dynamo, ingest, query, score
from tests.conftest import RAW_BUCKET
from tests.unit.test_alert import make_score, stream_record
from tests.unit.test_db import connect as connect  # noqa: F401  (pytest fixtures)
from tests.unit.test_db import settings as settings  # noqa: F401
from tests.unit.test_score import make_transaction, record


class Context:
    aws_request_id = "test-request-id"


class TestDatabaseCleanUp:
    def test_error_while_closing_the_connection_is_swallowed(self, connect):  # noqa: F811
        connection = db.get_connection()
        connection.close.side_effect = psycopg2.InterfaceError("already closed")
        db.close_connection()                      # must not raise
        assert db.get_connection() is not connection

    def test_connection_found_closed_after_rollback_is_discarded(self, connect):  # noqa: F811
        connection = db.get_connection()

        def rollback():
            connection.closed = 1                  # the server dropped us during the rollback

        connection.rollback.side_effect = rollback
        with pytest.raises(ValueError):
            with db.transaction():
                raise ValueError("bug in calling code")
        assert db.get_connection() is not connection

    def test_error_while_closing_the_cursor_does_not_hide_the_result(self, connect):  # noqa: F811
        connection = db.get_connection()
        connection.cursor.return_value.close.side_effect = psycopg2.InterfaceError("cursor gone")
        connection.cursor.return_value.fetchone.return_value = {"ok": 1}
        assert db.fetch_one("SELECT 1 AS ok") == {"ok": 1}
        connection.commit.assert_called_once()


class TestAuditTrailFailures:
    """The journey is an audit trail. Failing to write it must never fail the real work."""

    def test_alert_is_still_sent_and_marked_when_the_journey_write_fails(self, mail, capsys):
        with patch.object(dynamo, "put_event", side_effect=dynamo.DynamoError("throttled")):
            result = alert.handler({"Records": [stream_record(make_score(1))]}, Context())
        assert result["sent"] == 1 and len(mail) == 1
        assert dynamo.alert_already_sent("ACC-1007", "2026-09-15T13:10:00Z", "TXN-20260915-000001")
        assert '"journey_write_failed"' in capsys.readouterr().out

    def test_send_failure_is_still_raised_when_the_journey_write_also_fails(self, mail, monkeypatch):
        monkeypatch.setenv("FINTRUST_ALERT_SENDER", "not-verified@example.com")
        item = stream_record(make_score(1))
        with patch.object(dynamo, "put_event", side_effect=dynamo.DynamoError("throttled")):
            with pytest.raises(alert.AlertError):
                alert.handler({"Records": [item]}, Context())
        assert len(mail) == 0

    def test_scoring_failure_is_still_reported_when_the_journey_write_also_fails(self, events_table, capsys):
        transaction = make_transaction(1)
        with patch.object(score, "score_transaction", side_effect=RuntimeError("database down")), \
             patch.object(dynamo, "put_event", side_effect=dynamo.DynamoError("throttled")):
            result = score.handler({"Records": [record(transaction)]}, Context())
        assert result == {"batchItemFailures": [{"itemIdentifier": f"msg-{transaction.transaction_id}"}]}
        assert '"journey_write_failed"' in capsys.readouterr().out


class TestJourney:
    def test_journey_follows_every_page_of_results(self, events_table):
        """DynamoDB returns at most 1 MB per query; the rest must be fetched with LastEvaluatedKey."""
        pages = [
            {"Items": [{"record_type": "EVENT", "stage": "RECEIVED", "at": "2026-10-01T08:00:00.000Z"}],
             "LastEvaluatedKey": {"account_id": "ACC-1001", "tx_timestamp": "x"}},
            {"Items": [{"record_type": "EVENT", "stage": "QUEUED", "at": "2026-10-01T08:00:01.000Z"}]},
        ]
        fake_table = MagicMock()
        fake_table.query.side_effect = pages
        with patch.object(dynamo, "table", return_value=fake_table):
            journey = dynamo.get_journey("TXN-1")
        assert [event["stage"] for event in journey["events"]] == ["RECEIVED", "QUEUED"]
        assert fake_table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == pages[0]["LastEvaluatedKey"]

    def test_status_is_received_before_the_row_reaches_the_queue(self, events_table):
        events = [{"stage": "RECEIVED"}, {"stage": "VALIDATED"}]
        assert query.journey_status(None, events) == "RECEIVED"


class TestUnreadableFile:
    def test_file_the_csv_reader_cannot_parse_is_rejected_not_crashed_on(self, pipeline):
        # One "cell" far larger than any real value makes Python's csv module raise csv.Error.
        header = "transaction_id,account_id,tx_date,amount,currency,tx_type,channel,merchant_name,country_code,description"
        pipeline.upload("uploads/2026-10-07/huge-cell.csv", header + "\n" + "x" * 200_000 + "\n")
        summary = ingest.handler(pipeline.event("uploads/2026-10-07/huge-cell.csv", RAW_BUCKET), Context())["files"][0]
        assert summary["status"] == "FILE_REJECTED"
        assert "not a readable CSV" in summary["reason"]
