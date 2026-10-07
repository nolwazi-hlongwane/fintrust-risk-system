"""Unit tests for custom metrics (CloudWatch Embedded Metric Format).

Two things are tested: the shape of the line (CloudWatch ignores a line that is
not exactly right, silently), and that each Lambda publishes the right counts.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fintrust import alert, ingest, metrics, score
from tests.unit.test_alert import mail as mail  # noqa: F401  (pytest fixture)
from tests.unit.test_alert import make_score, stream_record
from tests.unit.test_score import fake_score, make_transaction, record

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class Context:
    aws_request_id = "test-request-id"


def emitted(capsys):
    """Every metrics line printed so far, merged into one dict of name -> list of values."""
    found = {}
    for line in capsys.readouterr().out.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if "_aws" in entry:
            for definition in entry["_aws"]["CloudWatchMetrics"][0]["Metrics"]:
                found.setdefault(definition["Name"], []).append(entry[definition["Name"]])
    return found


@pytest.fixture
def no_ssm(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")


@pytest.mark.usefixtures("no_ssm")
class TestFormat:
    def test_record_has_the_shape_cloudwatch_requires(self):
        entry = metrics.build({"RowsRead": 15, "RowsQueued": 14}, now_ms=1_780_000_000_000)
        assert entry["_aws"]["Timestamp"] == 1_780_000_000_000
        directive = entry["_aws"]["CloudWatchMetrics"][0]
        assert directive["Namespace"] == "FinTrust/ETL"
        assert directive["Dimensions"] == [["Environment"]]
        assert directive["Metrics"] == [{"Name": "RowsRead", "Unit": "Count"}, {"Name": "RowsQueued", "Unit": "Count"}]
        # every metric named in the directive, and the dimension, must be a top-level value
        assert entry["RowsRead"] == 15 and entry["RowsQueued"] == 14
        assert entry["Environment"] == "test"

    def test_timestamp_defaults_to_now_in_milliseconds(self):
        stamp = metrics.build({"RowsRead": 1})["_aws"]["Timestamp"]
        assert 1_700_000_000_000 < stamp < 4_000_000_000_000

    def test_namespace_comes_from_config(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_METRICS_NAMESPACE", "FinTrust/Other")
        assert metrics.build({"RowsRead": 1})["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "FinTrust/Other"

    def test_unknown_metric_name_is_refused(self):
        with pytest.raises(ValueError, match="RowsRaed"):
            metrics.build({"RowsRaed": 1})

    def test_zero_is_published(self):
        assert metrics.build({"ScoringFailures": 0})["ScoringFailures"] == 0

    def test_emit_prints_exactly_one_json_line(self, capsys, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        metrics.emit(AlertsSent=2, AlertFailures=0)
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["AlertsSent"] == 2

    def test_free_tier_limit_of_ten_custom_metrics(self):
        assert len(metrics.METRICS) <= 10
        assert len(set(metrics.METRICS)) == len(metrics.METRICS)

    def test_no_personal_information_in_a_metric_line(self):
        entry = metrics.build({"RowsRead": 1})
        assert set(entry) == {"_aws", "Environment", "message", "RowsRead"}


class TestIngestMetrics:
    def run(self, pipeline, body, key="uploads/2026-10-07/batch.csv"):
        pipeline.upload(key, body)
        return ingest.handler(pipeline.event(key), Context())

    def test_valid_file(self, pipeline, capsys):
        self.run(pipeline, (DATA_DIR / "sample_transactions_valid.csv").read_text())
        assert emitted(capsys) == {
            "RowsRead": [15], "RowsQueued": [15], "RowsQuarantined": [0], "FilesRejected": [0]}

    def test_mixed_file_counts_add_up(self, pipeline, capsys):
        self.run(pipeline, (DATA_DIR / "sample_transactions_mixed.csv").read_text())
        found = emitted(capsys)
        assert found == {"RowsRead": [11], "RowsQueued": [4], "RowsQuarantined": [7], "FilesRejected": [0]}
        assert found["RowsRead"][0] == found["RowsQueued"][0] + found["RowsQuarantined"][0]

    def test_rejected_file(self, pipeline, capsys):
        self.run(pipeline, "this,is,not,the,right,header\n1,2,3,4,5,6\n")
        assert emitted(capsys) == {
            "RowsRead": [0], "RowsQueued": [0], "RowsQuarantined": [0], "FilesRejected": [1]}

    def test_nothing_is_published_when_the_file_must_be_retried(self, pipeline, capsys):
        with patch.object(ingest, "send_to_queue", side_effect=lambda valid, *_: ([], list(valid))):
            with pytest.raises(ingest.IngestError):
                self.run(pipeline, (DATA_DIR / "sample_transactions_valid.csv").read_text())
        assert emitted(capsys) == {}


class TestScoreMetrics:
    def run(self, transactions, outcomes, capsys):
        def scorer(transaction):
            outcome = outcomes.get(transaction.transaction_id, (fake_score(transaction), True))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(score, "score_transaction", side_effect=scorer):
            score.handler({"Records": [record(t) for t in transactions]}, Context())
        return emitted(capsys)

    def test_counts_scored_and_high_risk(self, events_table, capsys):
        low, high = make_transaction(1), make_transaction(2, "ACC-1003")
        found = self.run([low, high], {high.transaction_id: (fake_score(high, "HIGH", 70), True)}, capsys)
        assert found == {"TransactionsScored": [2], "HighRiskTransactions": [1], "ScoringFailures": [0]}

    def test_redelivered_transaction_is_not_counted_twice(self, events_table, capsys):
        high = make_transaction(1)
        found = self.run([high], {high.transaction_id: (fake_score(high, "HIGH", 70), False)}, capsys)
        assert found == {"TransactionsScored": [0], "HighRiskTransactions": [0], "ScoringFailures": [0]}

    def test_failure_is_counted_once_and_held_back_messages_are_not(self, events_table, capsys):
        first, second, other = make_transaction(1), make_transaction(2), make_transaction(3, "ACC-1003")
        found = self.run([first, second, other], {first.transaction_id: RuntimeError("database down")}, capsys)
        # `second` is for the same account as `first`: held back to keep order, not a failure of its own.
        assert found == {"TransactionsScored": [1], "HighRiskTransactions": [0], "ScoringFailures": [1]}


class TestAlertMetrics:
    def test_sent(self, mail, capsys):  # noqa: F811
        alert.handler({"Records": [stream_record(make_score(1)), stream_record(make_score(2))]}, Context())
        assert emitted(capsys) == {"AlertsSent": [2], "AlertFailures": [0]}

    def test_duplicate_is_not_counted_as_sent(self, mail, capsys):  # noqa: F811
        item = stream_record(make_score(1))
        alert.handler({"Records": [item]}, Context())
        capsys.readouterr()
        alert.handler({"Records": [item]}, Context())
        assert emitted(capsys) == {"AlertsSent": [0], "AlertFailures": [0]}

    def test_failure_is_published_before_the_retry_error(self, mail, capsys, monkeypatch):  # noqa: F811
        monkeypatch.setenv("FINTRUST_ALERT_SENDER", "not-verified@example.com")
        with pytest.raises(alert.AlertError):
            alert.handler({"Records": [stream_record(make_score(1))]}, Context())
        assert emitted(capsys) == {"AlertsSent": [0], "AlertFailures": [1]}
