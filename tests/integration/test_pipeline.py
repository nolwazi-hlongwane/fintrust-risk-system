"""Integration test: the whole pipeline in one process.

    CSV in S3 -> ingest Lambda -> SQS FIFO -> score Lambda -> PostgreSQL + DynamoDB
              -> alert Lambda -> SES,   then read back through the API Lambda.

The four real handlers run against moto (S3, SQS, DynamoDB, SES) and a real
PostgreSQL. The only thing replaced is the AWS "glue" that moves an event from
one service to the next Lambda; the Pump class below does that job, following
the same rules AWS does:

    * the queue has the same redrive policy as the deployed one (3 receives,
      then the dead-letter queue), and SQS itself moves the message
    * a message is deleted only if the score Lambda did not report it as failed
    * the alert Lambda only sees SCORE items with risk_level HIGH (the stream filter)
    * each Lambda logs in to the database as its own user

The same flow is checked on the real account by the scripts/verify_*.py scripts.
"""

import csv
import io
import json

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer

from fintrust import alert, dynamo, ingest, query, score
from tests.conftest import QUARANTINE_BUCKET, REGION

HEADER = "transaction_id,account_id,tx_date,amount,currency,tx_type,channel,merchant_name,country_code,description"
LOW = "TXN-TEST-PIPE-0001,ACC-1001,2026-10-03T09:00:00+02:00,250.00,ZAR,DEBIT,POS,Checkers,ZA,Groceries"
MEDIUM = "TXN-TEST-PIPE-0002,ACC-1003,2026-10-03T09:05:00+02:00,61000.00,ZAR,CREDIT,EFT,Employer,ZA,Salary"
HIGH = "TXN-TEST-PIPE-0003,ACC-1007,2026-10-03T09:10:00+02:00,88750.00,ZAR,DEBIT,EFT,Dubai Marine,AE,Supplier payment"
BAD_AMOUNT = "TXN-TEST-PIPE-0004,ACC-1001,2026-10-03T09:15:00+02:00,-5.00,ZAR,DEBIT,POS,Shop,ZA,Negative amount"
BAD_ACCOUNT = "TXN-TEST-PIPE-0005,1001,2026-10-03T09:20:00+02:00,10.00,ZAR,DEBIT,POS,Shop,ZA,Malformed account"
NOT_ONBOARDED = "TXN-TEST-PIPE-0006,ACC-9990,2026-10-03T09:25:00+02:00,400.00,ZAR,DEBIT,POS,Shop,ZA,Account not onboarded yet"
SAME_ACCOUNT_LATER = "TXN-TEST-PIPE-0007,ACC-9990,2026-10-03T09:30:00+02:00,75.00,ZAR,DEBIT,POS,Shop,ZA,Second for that account"

MAX_RECEIVES = 3


class Context:
    aws_request_id = "pipeline-test"


def csv_file(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


class Pump:
    """Moves events between the Lambdas the way S3, SQS and DynamoDB Streams do."""

    def __init__(self, pipeline, as_db_user, queue_url, dlq_url):
        self.pipeline, self.as_db_user = pipeline, as_db_user
        self.sqs, self.queue_url, self.dlq_url = pipeline.sqs, queue_url, dlq_url
        self.alerted = set()

    # S3 "object created" notification -> ingest Lambda
    def upload(self, key, body):
        self.pipeline.upload(key, body)
        return ingest.handler(self.pipeline.event(key), Context())["files"][0]

    # SQS event source mapping -> score Lambda (one poll)
    def score_once(self):
        messages = self.sqs.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=10, AttributeNames=["All"], WaitTimeSeconds=0
        ).get("Messages", [])
        if not messages:
            return 0, 0
        self.as_db_user("fintrust_score")
        records = [{"messageId": m["MessageId"], "body": m["Body"], "attributes": m["Attributes"]} for m in messages]
        failed = {item["itemIdentifier"] for item in score.handler({"Records": records}, Context())["batchItemFailures"]}
        for message in messages:
            if message["MessageId"] in failed:
                # A failed message becomes visible again straight away (in AWS: after the visibility timeout).
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0)
            else:
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
        return len(messages), len(failed)

    def score_until_quiet(self, limit=20):
        polls = 0
        while self.score_once()[0] and polls < limit:
            polls += 1
        return polls

    # DynamoDB stream, filtered to HIGH scores -> alert Lambda
    def deliver_stream(self):
        serializer = TypeSerializer()
        records = []
        for item in dynamo.table().scan()["Items"]:
            if item["record_type"] == "SCORE" and item["risk_level"] == "HIGH" and item["tx_timestamp"] not in self.alerted:
                self.alerted.add(item["tx_timestamp"])
                image = {name: serializer.serialize(value) for name, value in item.items()}
                records.append({"eventName": "INSERT", "dynamodb": {"NewImage": image}})
        return alert.handler({"Records": records}, Context()) if records else None

    def run(self):
        self.score_until_quiet()
        self.deliver_stream()

    # API Gateway proxy event -> query Lambda
    def api(self, method, resource, params=None, path=None, body=None):
        self.as_db_user("fintrust_query")
        response = query.handler(
            {"httpMethod": method, "resource": resource, "queryStringParameters": params, "pathParameters": path,
             "body": json.dumps(body) if body is not None else None, "headers": {"Content-Type": "application/json"},
             "isBase64Encoded": False, "requestContext": {"requestId": "pipeline-test"}},
            Context(),
        )
        return response["statusCode"], json.loads(response["body"])

    def journey(self, transaction_id):
        return self.api("GET", "/transactions/{transaction_id}/journey", path={"transaction_id": transaction_id})[1]

    def waiting(self, queue_url):
        attributes = self.sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
        return int(attributes["ApproximateNumberOfMessages"])

    # What `aws sqs start-message-move-task` (scripts/redrive_dlq.sh) does: back to the source queue.
    def redrive(self):
        moved = 0
        while True:
            messages = self.sqs.receive_message(
                QueueUrl=self.dlq_url, MaxNumberOfMessages=10, AttributeNames=["All"]).get("Messages", [])
            if not messages:
                return moved
            for message in messages:
                self.sqs.send_message(
                    QueueUrl=self.queue_url, MessageBody=message["Body"],
                    MessageGroupId=message["Attributes"]["MessageGroupId"],
                    MessageDeduplicationId=f"redrive-{message['MessageId']}",
                )
                self.sqs.delete_message(QueueUrl=self.dlq_url, ReceiptHandle=message["ReceiptHandle"])
                moved += 1


@pytest.fixture
def pump(pipeline, mail, as_db_user, admin, monkeypatch):
    """The fake AWS resources wired like the deployed ones, plus a clean test area in the ledger."""
    sqs = boto3.client("sqs", region_name=REGION)
    dlq_url = sqs.create_queue(QueueName="pipeline-dlq.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    queue_url = sqs.create_queue(
        QueueName="pipeline.fifo",
        Attributes={
            "FifoQueue": "true",
            "VisibilityTimeout": "60",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEIVES}),
        },
    )["QueueUrl"]
    monkeypatch.setenv("FINTRUST_QUEUE_URL", queue_url)
    monkeypatch.setenv("FINTRUST_MAX_RECEIVE_COUNT", str(MAX_RECEIVES))

    yield Pump(pipeline, as_db_user, queue_url, dlq_url)

    # Remove this module's rows before the account they may belong to.
    with admin.cursor() as cursor:
        cursor.execute("DELETE FROM alerts WHERE transaction_id LIKE 'TXN-TEST-%'")
        cursor.execute("DELETE FROM risk_events WHERE transaction_id LIKE 'TXN-TEST-%' OR account_id = 'ACC-9990'")
        cursor.execute("DELETE FROM transactions WHERE transaction_id LIKE 'TXN-TEST-%'")
        cursor.execute("DELETE FROM accounts WHERE account_id = 'ACC-9990'")
        cursor.execute("DELETE FROM customers WHERE customer_id = 'CUST-9990'")


def ledger_rows(admin, pattern="TXN-TEST-PIPE-%"):
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT transaction_id, risk_score, risk_level FROM transactions WHERE transaction_id LIKE %s ORDER BY 1",
            (pattern,),
        )
        return cursor.fetchall()


def onboard_account(admin):
    with admin.cursor() as cursor:
        cursor.execute(
            "INSERT INTO customers (customer_id, full_name, email, phone_number) "
            "VALUES ('CUST-9990', 'Pipeline Test Customer', 'pipeline.test@example.com', '+27110000000')")
        cursor.execute(
            "INSERT INTO accounts (customer_id, account_id, account_type, opened_date) "
            "VALUES ('CUST-9990', 'ACC-9990', 'CHEQUE', '2026-09-01')")


class TestHappyPath:
    def test_file_with_good_and_bad_rows_flows_to_the_right_places(self, pump, mail, admin):
        summary = pump.upload("uploads/2026-10-03/batch.csv", csv_file(LOW, MEDIUM, HIGH, BAD_AMOUNT, BAD_ACCOUNT))
        pump.run()

        # ingest: the control total holds
        assert (summary["rows_read"], summary["rows_queued"], summary["rows_quarantined"]) == (5, 3, 2)

        # quarantine: the two bad rows, each with its reason, and nothing else
        rejected = list(csv.DictReader(io.StringIO(
            pump.pipeline.read(QUARANTINE_BUCKET, "uploads/2026-10-03/batch.rejected.csv"))))
        assert [row["transaction_id"] for row in rejected] == ["TXN-TEST-PIPE-0004", "TXN-TEST-PIPE-0005"]
        assert all(row["errors"] for row in rejected)

        # ledger: the three good rows with the right scores; the bad rows never arrived
        assert ledger_rows(admin) == [
            ("TXN-TEST-PIPE-0001", 0, "LOW"), ("TXN-TEST-PIPE-0002", 50, "MEDIUM"), ("TXN-TEST-PIPE-0003", 70, "HIGH")]

        # alert: exactly one email, for the HIGH transaction, with a masked account
        assert len(mail) == 1
        assert mail[0].subject == "[FinTrust] HIGH risk transaction TXN-TEST-PIPE-0003 (score 70)"
        assert "ACC-**07" in mail[0].body and "ACC-1007" not in mail[0].body

        # queues: nothing left behind
        assert pump.waiting(pump.queue_url) == 0 and pump.waiting(pump.dlq_url) == 0

    def test_api_shows_what_the_pipeline_stored(self, pump, mail):
        pump.upload("uploads/2026-10-03/batch.csv", csv_file(LOW, MEDIUM, HIGH))
        pump.run()

        journey = pump.journey("TXN-TEST-PIPE-0003")
        assert journey["status"] == "ALERTED"
        assert [event["stage"] for event in journey["events"]] == [
            "RECEIVED", "VALIDATED", "QUEUED", "SCORED", "ALERT_SENT"]
        assert {rule["rule"] for rule in journey["score"]["rules"]} == {"HIGH_AMOUNT", "CROSS_BORDER"}
        assert pump.journey("TXN-TEST-PIPE-0001")["status"] == "SCORED"

        status, found = pump.api("GET", "/transactions", {"account_id": "ACC-1007", "risk_level": "HIGH"})
        assert status == 200 and "TXN-TEST-PIPE-0003" in [item["transaction_id"] for item in found["items"]]

        status, alerts = pump.api("GET", "/alerts", {"status": "OPEN", "limit": "100"})
        mine = [item for item in alerts["items"] if item["transaction_id"] == "TXN-TEST-PIPE-0003"]
        assert status == 200 and len(mine) == 1

        status, resolved = pump.api(
            "POST", "/resolve", body={"alert_id": mine[0]["alert_id"], "resolved_by": "pipeline.test"})
        assert (status, resolved["status"]) == (200, "RESOLVED")

    def test_uploading_the_same_file_twice_changes_nothing(self, pump, mail, admin):
        body = csv_file(LOW, HIGH)
        pump.upload("uploads/2026-10-03/first.csv", body)
        pump.run()
        before = ledger_rows(admin)

        pump.upload("uploads/2026-10-03/again.csv", body)
        pump.run()

        assert ledger_rows(admin) == before          # no duplicate ledger rows
        assert len(mail) == 1                        # no second email
        with admin.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM alerts WHERE transaction_id = 'TXN-TEST-PIPE-0003'")
            assert cursor.fetchone()[0] == 1         # no second alert


class TestDeadLetterQueue:
    """Break it and recover: the scenario shown in the live demo."""

    def test_failed_message_is_dead_lettered_after_three_attempts_and_nothing_is_lost(self, pump, admin):
        pump.upload("uploads/2026-10-03/batch.csv", csv_file(LOW, NOT_ONBOARDED))
        pump.score_until_quiet()

        # the good transaction was not held up by the failing one (different account = different message group)
        assert ledger_rows(admin) == [("TXN-TEST-PIPE-0001", 0, "LOW")]

        # the failing message was tried exactly three times, then SQS moved it to the dead-letter queue
        journey = pump.journey("TXN-TEST-PIPE-0006")
        failures = [event for event in journey["events"] if event["stage"] == "SCORE_FAILED"]
        assert len(failures) == MAX_RECEIVES
        assert failures[0]["detail"].startswith("Attempt 1 of 3")
        assert "dead-letter queue" in failures[-1]["detail"]
        assert journey["status"] == "DEAD_LETTERED" and journey["score"] is None
        assert pump.waiting(pump.dlq_url) == 1 and pump.waiting(pump.queue_url) == 0

    def test_recover_by_fixing_the_cause_and_redriving(self, pump, admin, mail):
        pump.upload("uploads/2026-10-03/batch.csv", csv_file(NOT_ONBOARDED))
        pump.score_until_quiet()
        assert pump.waiting(pump.dlq_url) == 1 and ledger_rows(admin) == []

        onboard_account(admin)                       # fix the cause
        assert pump.redrive() == 1                   # then move the message back
        pump.run()

        assert ledger_rows(admin) == [("TXN-TEST-PIPE-0006", 0, "LOW")]
        journey = pump.journey("TXN-TEST-PIPE-0006")
        assert journey["status"] == "SCORED"
        assert [event["stage"] for event in journey["events"]][-1] == "SCORED"
        assert pump.waiting(pump.dlq_url) == 0 and pump.waiting(pump.queue_url) == 0

    def test_order_is_kept_for_the_failing_account(self, pump, admin):
        pump.upload("uploads/2026-10-03/batch.csv", csv_file(NOT_ONBOARDED, SAME_ACCOUNT_LATER, LOW))
        pump.score_until_quiet()

        # both messages for the unknown account ended in the dead-letter queue; neither jumped the queue
        assert ledger_rows(admin) == [("TXN-TEST-PIPE-0001", 0, "LOW")]
        assert pump.waiting(pump.dlq_url) == 2

        onboard_account(admin)
        assert pump.redrive() == 2
        pump.run()
        assert [row[0] for row in ledger_rows(admin)] == [
            "TXN-TEST-PIPE-0001", "TXN-TEST-PIPE-0006", "TXN-TEST-PIPE-0007"]

    def test_email_failure_never_undoes_the_ledger(self, pump, admin, monkeypatch):
        monkeypatch.setenv("FINTRUST_ALERT_SENDER", "not-verified@example.com")
        pump.upload("uploads/2026-10-03/batch.csv", csv_file(HIGH))
        pump.score_until_quiet()
        with pytest.raises(alert.AlertError):
            pump.deliver_stream()

        assert ledger_rows(admin) == [("TXN-TEST-PIPE-0003", 70, "HIGH")]
        status, alerts = pump.api("GET", "/alerts", {"status": "OPEN", "limit": "100"})
        assert "TXN-TEST-PIPE-0003" in [item["transaction_id"] for item in alerts["items"]]
        assert pump.journey("TXN-TEST-PIPE-0003")["events"][-1]["stage"] == "ALERT_FAILED"
