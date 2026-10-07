"""Unit tests for the alert Lambda, against moto fakes of SES and DynamoDB."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from moto.core import DEFAULT_ACCOUNT_ID
from moto.ses import ses_backends

from fintrust import alert, dynamo
from fintrust.models import RiskScore, RuleHit

SENDER = "compliance-alerts@example.com"
RECIPIENT = "compliance-team@example.com"
STAMP = "2026-09-15T13:10:00Z"


class Context:
    aws_request_id = "test-request-id"


def make_score(number=1, account="ACC-1007", level="HIGH", points=70):
    hits = (RuleHit("HIGH_AMOUNT", 50, "Amount 104500.00 is above 50000"),
            RuleHit("CROSS_BORDER", 20, "Country AE is not ZA")) if points >= 70 else ()
    return RiskScore(f"TXN-20260915-{number:06d}", account, STAMP, points, level, hits,
                     scored_at=datetime(2026, 9, 15, 13, 10, 4, tzinfo=timezone.utc))


def stream_record(score=None, event_name="INSERT", item=None):
    """A DynamoDB stream record as Lambda receives it, built from a real stored item."""
    if item is None:
        item = dynamo.put_score(score)
    image = {key: TypeSerializer().serialize(value) for key, value in item.items()}
    return {"eventName": event_name, "dynamodb": {"NewImage": image}}


@pytest.fixture
def mail(events_table, monkeypatch):
    """Fake SES with a verified sender, and config pointing at it. Returns the list of sent messages."""
    monkeypatch.setenv("FINTRUST_ALERT_SENDER", SENDER)
    monkeypatch.setenv("FINTRUST_ALERT_RECIPIENT", RECIPIENT)
    boto3.client("ses", region_name="eu-north-1").verify_email_identity(EmailAddress=SENDER)
    return ses_backends[DEFAULT_ACCOUNT_ID]["eu-north-1"].sent_messages


def run(*records):
    return alert.handler({"Records": list(records)}, Context())


class TestSending:
    def test_high_risk_score_sends_one_email(self, mail):
        assert run(stream_record(make_score())) == {"sent": 1, "duplicate": 0, "ignored": 0}
        assert len(mail) == 1
        assert mail[0].source == SENDER
        assert mail[0].destinations["ToAddresses"] == [RECIPIENT]

    def test_subject_names_the_transaction_and_score(self, mail):
        run(stream_record(make_score()))
        assert mail[0].subject == "[FinTrust] HIGH risk transaction TXN-20260915-000001 (score 70)"

    def test_body_explains_the_score_rule_by_rule(self, mail):
        run(stream_record(make_score()))
        body = mail[0].body
        assert "HIGH_AMOUNT" in body and "Amount 104500.00 is above 50000" in body
        assert "CROSS_BORDER" in body and "Country AE is not ZA" in body
        assert "70" in body

    def test_account_number_is_masked_everywhere(self, mail):
        run(stream_record(make_score()))
        subject, text, html = alert.build_email(dynamo.get_score("ACC-1007", STAMP, "TXN-20260915-000001"))
        for part in (subject, text, html, mail[0].body):
            assert "ACC-1007" not in part
        assert "ACC-**07" in text and "ACC-**07" in html

    def test_html_is_escaped(self, mail):
        score = dynamo.put_score(make_score())
        score["rules"] = [{"rule": "HIGH_AMOUNT", "points": 50, "detail": "<script>alert(1)</script>"}]
        _subject, _text, html = alert.build_email(score)
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_alert_step_is_added_to_the_journey(self, mail):
        run(stream_record(make_score()))
        events = dynamo.get_journey("TXN-20260915-000001")["events"]
        assert events[-1]["stage"] == "ALERT_SENT"
        assert events[-1]["detail"] == "Email sent to the compliance team"

    def test_several_high_risk_scores_in_one_batch(self, mail):
        result = run(*[stream_record(make_score(n)) for n in (1, 2, 3)])
        assert result["sent"] == 3 and len(mail) == 3

    def test_log_has_no_email_address_or_full_account(self, mail, capsys):
        run(stream_record(make_score()))
        output = capsys.readouterr().out
        sent = next(json.loads(line) for line in output.splitlines() if '"alert_sent"' in line)
        assert sent["transaction_id"] == "TXN-20260915-000001" and sent["ses_message_id"]
        assert "example.com" not in output and "ACC-1007" not in output


class TestExactlyOnce:
    def test_score_written_again_does_not_send_a_second_email(self, mail):
        record = stream_record(make_score())
        run(record)
        modified = stream_record(make_score(), event_name="MODIFY")
        assert run(modified) == {"sent": 0, "duplicate": 1, "ignored": 0}
        assert len(mail) == 1

    def test_same_batch_delivered_twice_sends_once(self, mail):
        record = stream_record(make_score())
        run(record)
        run(record)
        assert len(mail) == 1

    def test_same_second_different_transactions_both_alert(self, mail):
        run(stream_record(make_score(1)), stream_record(make_score(2)))
        assert len(mail) == 2


class TestIgnored:
    @pytest.mark.parametrize("level, points", [("LOW", 0), ("MEDIUM", 50)])
    def test_lower_risk_scores_send_nothing(self, mail, level, points):
        assert run(stream_record(make_score(level=level, points=points)))["ignored"] == 1
        assert mail == []

    def test_journey_events_send_nothing(self, mail):
        item = dynamo.put_event("ACC-1007", STAMP, "TXN-20260915-000001", "QUEUED")
        assert run(stream_record(item=item))["ignored"] == 1
        assert mail == []

    def test_alert_markers_do_not_trigger_alerts(self, mail):
        item = dynamo.mark_alert_sent("ACC-1007", STAMP, "TXN-20260915-000001", "ses-id")
        assert run(stream_record(item=item))["ignored"] == 1

    def test_deleted_items_send_nothing(self, mail):
        record = stream_record(make_score())
        record["eventName"] = "REMOVE"
        del record["dynamodb"]["NewImage"]
        assert run(record)["ignored"] == 1
        assert mail == []

    def test_empty_event(self, mail):
        assert alert.handler({}, Context()) == {"sent": 0, "duplicate": 0, "ignored": 0}


class TestFailure:
    REJECTED = ClientError({"Error": {"Code": "MessageRejected", "Message": "Email address is not verified"}},
                           "SendEmail")

    def test_ses_failure_raises_so_the_stream_retries(self, mail):
        record = stream_record(make_score())
        with patch.object(alert._ses, "send_email", side_effect=self.REJECTED):
            with pytest.raises(alert.AlertError, match="1 alert"):
                run(record)
        assert mail == []

    def test_failed_alert_is_recorded_and_not_marked_as_sent(self, mail):
        record = stream_record(make_score())
        with patch.object(alert._ses, "send_email", side_effect=self.REJECTED):
            with pytest.raises(alert.AlertError):
                run(record)
        events = dynamo.get_journey("TXN-20260915-000001")["events"]
        assert events[-1]["stage"] == "ALERT_FAILED"
        assert dynamo.alert_already_sent("ACC-1007", STAMP, "TXN-20260915-000001") is False

    def test_retry_after_failure_sends_the_alert(self, mail):
        record = stream_record(make_score())
        with patch.object(alert._ses, "send_email", side_effect=self.REJECTED):
            with pytest.raises(alert.AlertError):
                run(record)
        assert run(record)["sent"] == 1
        assert len(mail) == 1

    def test_one_failure_does_not_stop_the_others_and_retry_does_not_resend_them(self, mail):
        good, bad = stream_record(make_score(1)), stream_record(make_score(2))
        real = alert._ses.send_email

        def fail_second(**arguments):
            if "000002" in arguments["Message"]["Subject"]["Data"]:
                raise self.REJECTED
            return real(**arguments)

        with patch.object(alert._ses, "send_email", side_effect=fail_second):
            with pytest.raises(alert.AlertError):
                run(bad, good)
        assert len(mail) == 1                      # the good one went despite the bad one
        assert run(bad, good) == {"sent": 1, "duplicate": 1, "ignored": 0}
        assert len(mail) == 2                      # the retry sent only the one that had failed

    def test_unverified_sender_is_a_failure(self, mail, monkeypatch):
        monkeypatch.setenv("FINTRUST_ALERT_SENDER", "not-verified@example.com")
        with pytest.raises(alert.AlertError):
            run(stream_record(make_score()))

    def test_failure_is_logged(self, mail, capsys):
        with patch.object(alert._ses, "send_email", side_effect=self.REJECTED):
            with pytest.raises(alert.AlertError):
                run(stream_record(make_score()))
        output = capsys.readouterr().out
        assert "alert_send_failed" in output and "not verified" in output

    def test_ses_client_is_created_at_module_level(self):
        assert alert._ses.meta.service_model.service_name == "ses"
