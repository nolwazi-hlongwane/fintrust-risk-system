"""Unit tests for the ingest Lambda, run end to end against moto fakes of
S3, SQS (FIFO) and DynamoDB."""

import csv
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from fintrust import dynamo, ingest
from tests.conftest import PROCESSED_BUCKET, QUARANTINE_BUCKET

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
HEADER = "transaction_id,account_id,tx_date,amount,currency,tx_type,channel,merchant_name,country_code,description"
GOOD_1 = "TXN-20261001-900001,ACC-1001,2026-10-01T08:15:22+02:00,412.50,ZAR,DEBIT,POS,Checkers Sandton,ZA,Groceries"
GOOD_2 = "TXN-20261002-900007,ACC-1007,2026-10-02T11:48:09+02:00,88750.00,ZAR,DEBIT,EFT,Dubai Marine,AE,Supplier payment"
BAD_AMOUNT = "TXN-20261005-910002,ACC-1003,2026-10-05T08:25:00+02:00,-150.00,ZAR,DEBIT,POS,Shoprite,ZA,Negative"
KEY = "uploads/2026-10-06/batch.csv"


class Context:
    aws_request_id = "test-request-id"


def csv_text(*lines):
    return "\n".join([HEADER, *lines]) + "\n"


def run(pipeline, body, key=KEY):
    pipeline.upload(key, body)
    return ingest.handler(pipeline.event(key), Context())["files"][0]


def parse(text):
    return list(csv.DictReader(io.StringIO(text)))


class TestValidFile:
    def test_summary_counts(self, pipeline):
        summary = run(pipeline, csv_text(GOOD_1, GOOD_2))
        assert summary["status"] == "PROCESSED"
        assert (summary["rows_read"], summary["rows_valid"], summary["rows_queued"]) == (2, 2, 2)
        assert summary["rows_quarantined"] == 0
        assert summary["journey_write_failures"] == 0

    def test_one_message_per_transaction_grouped_by_account(self, pipeline):
        run(pipeline, csv_text(GOOD_1, GOOD_2))
        messages = pipeline.messages()
        assert len(messages) == 2
        by_id = {message["body"]["transaction_id"]: message for message in messages}
        first = by_id["TXN-20261001-900001"]
        assert first["group"] == "ACC-1001"
        assert first["deduplication_id"] == "TXN-20261001-900001"
        assert by_id["TXN-20261002-900007"]["group"] == "ACC-1007"

    def test_message_carries_the_transaction_and_its_source(self, pipeline):
        run(pipeline, csv_text(GOOD_1))
        body = pipeline.messages()[0]["body"]
        assert body["amount"] == "412.50"
        assert body["tx_date"] == "2026-10-01T08:15:22+02:00"
        assert body["source_file"] == KEY and body["source_key"] == KEY

    def test_message_can_be_rebuilt_into_a_transaction(self, pipeline):
        from fintrust.models import Transaction

        run(pipeline, csv_text(GOOD_2))
        transaction = Transaction.from_message(pipeline.messages()[0]["body"])
        assert str(transaction.amount) == "88750.00"
        assert transaction.country_code == "AE"

    def test_pre_score_flags_amount_and_cross_border(self, pipeline):
        summary = run(pipeline, csv_text(GOOD_1, GOOD_2))
        by_id = {m["body"]["transaction_id"]: m["body"]["pre_score"] for m in pipeline.messages()}
        assert by_id["TXN-20261001-900001"] == {"points": 0, "flags": []}
        assert by_id["TXN-20261002-900007"] == {"points": 70, "flags": ["HIGH_AMOUNT", "CROSS_BORDER"]}
        assert summary["pre_score_flagged"] == 1

    def test_clean_rows_are_written_to_the_processed_bucket(self, pipeline):
        run(pipeline, csv_text(GOOD_1, GOOD_2))
        rows = parse(pipeline.read(PROCESSED_BUCKET, "uploads/2026-10-06/batch.clean.csv"))
        assert [row["transaction_id"] for row in rows] == ["TXN-20261001-900001", "TXN-20261002-900007"]
        assert rows[1]["pre_score"] == "70"
        assert rows[1]["pre_flags"] == "HIGH_AMOUNT|CROSS_BORDER"

    def test_nothing_is_quarantined(self, pipeline):
        run(pipeline, csv_text(GOOD_1))
        assert pipeline.keys(QUARANTINE_BUCKET) == []

    def test_journey_is_recorded_for_each_transaction(self, pipeline):
        run(pipeline, csv_text(GOOD_1))
        events = dynamo.get_journey("TXN-20261001-900001")["events"]
        assert [event["stage"] for event in events] == ["RECEIVED", "VALIDATED", "QUEUED"]
        assert events[0]["detail"] == f"{KEY} line 2"

    def test_all_rows_of_a_larger_file_are_queued(self, pipeline):
        lines = [
            f"TXN-20261001-{number:06d},ACC-{1001 + number % 4},2026-10-01T08:00:{number:02d}+02:00,"
            f"{10 + number}.00,ZAR,DEBIT,POS,Shop,ZA,Item"
            for number in range(37)
        ]
        summary = run(pipeline, csv_text(*lines))
        assert summary["rows_queued"] == 37
        assert len(pipeline.messages()) == 37

    def test_one_accounts_messages_keep_file_order(self, pipeline):
        lines = [
            f"TXN-20261001-{number:06d},ACC-1001,2026-10-01T08:00:{number:02d}+02:00,10.00,ZAR,DEBIT,POS,Shop,ZA,Item"
            for number in range(15)
        ]
        run(pipeline, csv_text(*lines))
        order = [message["body"]["transaction_id"] for message in pipeline.messages()]
        assert order == sorted(order)


class TestMixedFile:
    def test_bad_rows_are_quarantined_and_good_rows_continue(self, pipeline):
        summary = run(pipeline, csv_text(GOOD_1, BAD_AMOUNT, GOOD_2))
        assert (summary["rows_valid"], summary["rows_quarantined"], summary["rows_queued"]) == (2, 1, 2)
        assert len(pipeline.messages()) == 2

    def test_quarantine_file_gives_row_number_reason_and_original_values(self, pipeline):
        run(pipeline, csv_text(GOOD_1, BAD_AMOUNT))
        rows = parse(pipeline.read(QUARANTINE_BUCKET, "uploads/2026-10-06/batch.rejected.csv"))
        assert len(rows) == 1
        assert rows[0]["row_number"] == "3"
        assert "amount '-150.00' must be a positive number" in rows[0]["errors"]
        assert rows[0]["transaction_id"] == "TXN-20261005-910002"
        assert rows[0]["amount"] == "-150.00"

    def test_rejected_row_never_reaches_queue_processed_bucket_or_journey(self, pipeline):
        run(pipeline, csv_text(GOOD_1, BAD_AMOUNT))
        assert all(m["body"]["transaction_id"] != "TXN-20261005-910002" for m in pipeline.messages())
        assert "910002" not in pipeline.read(PROCESSED_BUCKET, "uploads/2026-10-06/batch.clean.csv")
        assert dynamo.get_journey("TXN-20261005-910002") == {"score": None, "events": []}

    def test_duplicate_transaction_id_in_one_file(self, pipeline):
        summary = run(pipeline, csv_text(GOOD_1, GOOD_1))
        assert (summary["rows_valid"], summary["rows_quarantined"]) == (1, 1)
        rows = parse(pipeline.read(QUARANTINE_BUCKET, "uploads/2026-10-06/batch.rejected.csv"))
        assert rows[0]["errors"] == "transaction_id appears more than once in this file"

    def test_row_with_wrong_number_of_values(self, pipeline):
        summary = run(pipeline, csv_text(GOOD_1, GOOD_2 + ",extra,values"))
        assert summary["rows_quarantined"] == 1
        rows = parse(pipeline.read(QUARANTINE_BUCKET, "uploads/2026-10-06/batch.rejected.csv"))
        assert "row has 12 values but the header has 10 columns" in rows[0]["errors"]

    def test_blank_lines_are_skipped(self, pipeline):
        summary = run(pipeline, csv_text(GOOD_1, "", ",,,,,,,,,", GOOD_2))
        assert (summary["rows_read"], summary["rows_valid"], summary["rows_quarantined"]) == (2, 2, 0)

    def test_file_with_only_bad_rows(self, pipeline):
        summary = run(pipeline, csv_text(BAD_AMOUNT))
        assert (summary["rows_valid"], summary["rows_quarantined"], summary["rows_queued"]) == (0, 1, 0)
        assert pipeline.messages() == []
        assert pipeline.keys(PROCESSED_BUCKET) == []

    def test_every_row_is_accounted_for(self, pipeline):
        summary = run(pipeline, csv_text(GOOD_1, BAD_AMOUNT, GOOD_2, GOOD_1))
        assert summary["rows_read"] == summary["rows_queued"] + summary["rows_quarantined"]

    def test_personal_information_in_free_text_is_redacted_everywhere(self, pipeline):
        line = ("TXN-20261005-910008,ACC-1010,2026-10-05T10:45:00+02:00,2500.00,ZAR,DEBIT,EFT,DStv,ZA,"
                "Paid for j.naidoo@example.com ref 8001015009087")
        summary = run(pipeline, csv_text(line))
        assert summary["warnings"] == 2
        body = pipeline.messages()[0]["body"]
        assert body["description"] == "Paid for [REDACTED] ref [REDACTED]"
        assert "j.naidoo" not in pipeline.read(PROCESSED_BUCKET, "uploads/2026-10-06/batch.clean.csv")

    def test_unneeded_personal_columns_are_never_copied(self, pipeline, capsys):
        body = (HEADER + ",customer_name,id_number\n" + GOOD_1 + ",Thandiwe Mokoena,8001015009087\n")
        run(pipeline, body)
        assert "Thandiwe" not in json.dumps(pipeline.messages())
        assert "Thandiwe" not in pipeline.read(PROCESSED_BUCKET, "uploads/2026-10-06/batch.clean.csv")
        output = capsys.readouterr().out
        assert "columns_ignored" in output
        assert "Thandiwe" not in output and "8001015009087" not in output


class TestSampleFiles:
    def test_valid_sample_file(self, pipeline):
        summary = run(pipeline, (DATA_DIR / "sample_transactions_valid.csv").read_text())
        assert (summary["rows_read"], summary["rows_valid"], summary["rows_quarantined"]) == (15, 15, 0)
        assert summary["pre_score_flagged"] == 6

    def test_mixed_sample_file(self, pipeline):
        summary = run(pipeline, (DATA_DIR / "sample_transactions_mixed.csv").read_text())
        assert (summary["rows_read"], summary["rows_valid"], summary["rows_quarantined"]) == (11, 4, 7)
        queued = sorted(m["body"]["transaction_id"] for m in pipeline.messages())
        assert queued == ["TXN-20261005-910001", "TXN-20261005-910005", "TXN-20261005-910008",
                          "TXN-20261005-910010"]
        rejected = parse(pipeline.read(QUARANTINE_BUCKET, "uploads/2026-10-06/batch.rejected.csv"))
        assert [row["row_number"] for row in rejected] == ["3", "4", "5", "7", "8", "10", "11"]
        several = next(row for row in rejected if row["row_number"] == "10")
        assert several["errors"].count(";") == 5


class TestRejectedFile:
    @pytest.mark.parametrize(
        "body, reason",
        [
            ("", "file is empty"),
            ("   \n\n", "file is empty"),
            ("transaction_id,amount\nTXN-1,5\n", "missing required column(s): account_id, tx_date, tx_type, "
                                                  "channel, country_code"),
            (b"\xff\xfe\x00bad bytes", "file is not UTF-8 text"),
        ],
    )
    def test_unusable_file_is_reported_and_nothing_is_queued(self, pipeline, body, reason):
        summary = run(pipeline, body)
        assert summary["status"] == "FILE_REJECTED"
        assert summary["reason"] == reason
        assert pipeline.messages() == []
        report = json.loads(pipeline.read(QUARANTINE_BUCKET, "uploads/2026-10-06/batch.file-rejected.json"))
        assert report["reason"] == reason and report["source_key"] == KEY

    def test_too_many_rows(self, pipeline, monkeypatch):
        monkeypatch.setattr(ingest, "MAX_ROWS", 2)
        summary = run(pipeline, csv_text(GOOD_1, GOOD_2, BAD_AMOUNT))
        assert summary["reason"] == "file has 3 rows; the limit is 2 per file"

    def test_file_too_large(self, pipeline, monkeypatch):
        monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 50)
        assert run(pipeline, csv_text(GOOD_1))["reason"] == "file is larger than 0 MB"

    def test_header_only_file_is_processed_with_zero_rows(self, pipeline):
        summary = run(pipeline, HEADER + "\n")
        assert summary["status"] == "PROCESSED" and summary["rows_read"] == 0

    def test_excel_byte_order_mark_is_handled(self, pipeline):
        summary = run(pipeline, ("﻿" + csv_text(GOOD_1)).encode("utf-8"))
        assert summary["rows_valid"] == 1

    def test_quoted_fields_with_commas(self, pipeline):
        line = 'TXN-20261001-900001,ACC-1001,2026-10-01T08:15:22+02:00,412.50,ZAR,DEBIT,POS,"Smith, Jones Inc",ZA,"a, b"'
        run(pipeline, csv_text(line))
        assert pipeline.messages()[0]["body"]["merchant_name"] == "Smith, Jones Inc"


class TestEventHandling:
    def test_key_with_spaces_is_decoded(self, pipeline):
        key = "uploads/october batch (final).csv"
        summary = run(pipeline, csv_text(GOOD_1), key=key)
        assert summary["source_key"] == key
        assert "uploads/october batch (final).clean.csv" in pipeline.keys(PROCESSED_BUCKET)

    def test_object_from_another_bucket_is_ignored(self, pipeline):
        result = ingest.handler(pipeline.event(KEY, bucket="someone-elses-bucket"), Context())
        assert result == {"files": []}

    def test_non_csv_object_is_ignored(self, pipeline):
        pipeline.upload("uploads/notes.txt", "hello")
        assert ingest.handler(pipeline.event("uploads/notes.txt"), Context()) == {"files": []}

    def test_several_files_in_one_event(self, pipeline):
        pipeline.upload("a.csv", csv_text(GOOD_1))
        pipeline.upload("b.csv", csv_text(GOOD_2))
        event = {"Records": pipeline.event("a.csv")["Records"] + pipeline.event("b.csv")["Records"]}
        assert [f["rows_queued"] for f in ingest.handler(event, Context())["files"]] == [1, 1]

    def test_missing_object_raises_so_lambda_retries(self, pipeline):
        with pytest.raises(ingest.IngestError, match="AWS error while processing"):
            ingest.handler(pipeline.event("uploads/not-there.csv"), Context())

    def test_empty_event(self, pipeline):
        assert ingest.handler({}, Context()) == {"files": []}

    def test_logs_are_json_with_request_id_and_no_personal_details(self, pipeline, capsys):
        run(pipeline, csv_text(GOOD_1, BAD_AMOUNT))
        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        processed = next(line for line in lines if line["message"] == "file_processed")
        assert processed["service"] == "ingest"
        assert processed["request_id"] == "test-request-id"
        assert processed["source_key"] == KEY
        assert (processed["rows_valid"], processed["rows_quarantined"]) == (1, 1)
        assert any(line["message"] == "rows_quarantined" and line["level"] == "WARNING" for line in lines)
        assert "Checkers" not in json.dumps(lines)


class TestFailureHandling:
    def test_queue_failure_raises_after_writing_what_it_could(self, pipeline):
        throttled = ClientError({"Error": {"Code": "RequestThrottled", "Message": "slow down"}}, "SendMessageBatch")
        pipeline.upload(KEY, csv_text(GOOD_1, BAD_AMOUNT))
        with patch.object(ingest._sqs, "send_message_batch", side_effect=throttled):
            with pytest.raises(ingest.IngestError, match="1 of 1 valid rows could not be queued"):
                ingest.handler(pipeline.event(KEY), Context())
        assert "uploads/2026-10-06/batch.rejected.csv" in pipeline.keys(QUARANTINE_BUCKET)
        events = dynamo.get_journey("TXN-20261001-900001")["events"]
        assert "QUEUED" not in [event["stage"] for event in events]

    def test_partially_failed_batch_raises(self, pipeline):
        real = ingest._sqs.send_message_batch

        def one_fails(**arguments):
            response = real(QueueUrl=arguments["QueueUrl"], Entries=arguments["Entries"][:1])
            response["Failed"] = [{"Id": "1", "Code": "InternalError", "Message": "boom", "SenderFault": False}]
            return response

        pipeline.upload(KEY, csv_text(GOOD_1, GOOD_2))
        with patch.object(ingest._sqs, "send_message_batch", side_effect=one_fails):
            with pytest.raises(ingest.IngestError, match="1 of 2"):
                ingest.handler(pipeline.event(KEY), Context())
        assert len(pipeline.messages()) == 1

    def test_retry_of_the_same_file_does_not_duplicate_messages(self, pipeline):
        pipeline.upload(KEY, csv_text(GOOD_1, GOOD_2))
        ingest.handler(pipeline.event(KEY), Context())
        ingest.handler(pipeline.event(KEY), Context())
        assert len(pipeline.messages()) == 2
        assert pipeline.keys(PROCESSED_BUCKET) == ["uploads/2026-10-06/batch.clean.csv"]

    def test_journey_store_failure_does_not_stop_ingestion(self, pipeline, capsys):
        pipeline.upload(KEY, csv_text(GOOD_1))
        with patch.object(dynamo, "put_event", side_effect=dynamo.DynamoError("table unavailable")):
            summary = ingest.handler(pipeline.event(KEY), Context())["files"][0]
        assert summary["rows_queued"] == 1
        assert summary["journey_write_failures"] == 3
        assert len(pipeline.messages()) == 1
        assert "journey_writes_failed" in capsys.readouterr().out

    def test_quarantine_bucket_failure_raises(self, pipeline):
        denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject")
        pipeline.upload(KEY, csv_text(BAD_AMOUNT))
        with patch.object(ingest._s3, "put_object", side_effect=denied):
            with pytest.raises(ingest.IngestError):
                ingest.handler(pipeline.event(KEY), Context())

    def test_clients_are_created_at_module_level(self):
        assert ingest._s3.meta.service_model.service_name == "s3"
        assert ingest._sqs.meta.service_model.service_name == "sqs"
