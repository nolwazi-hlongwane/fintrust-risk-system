"""Unit tests for the write routes of the query Lambda (POST /upload, /flag,
/resolve) and GET /alerts validation, with the database stubbed and S3 faked by
moto. The SQL and the flag_account function are tested against a real PostgreSQL
in tests/integration/test_write_api_integration.py."""

import base64
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from fintrust import db, query

RAW_BUCKET = "fintrust-capstone-raw-test"
HEADER = "transaction_id,account_id,tx_date,amount,currency,tx_type,channel,merchant_name,country_code,description"
ROW = "TXN-20261001-900001,ACC-1001,2026-10-01T08:15:22+02:00,412.50,ZAR,DEBIT,POS,Checkers Sandton,ZA,Groceries"
CSV = f"{HEADER}\n{ROW}\n"


class Context:
    aws_request_id = "lambda-request-id"


def call(method, resource, body=None, params=None, base64_encoded=False):
    if isinstance(body, dict | list):
        body = json.dumps(body)
    event = {
        "httpMethod": method, "resource": resource, "queryStringParameters": params, "pathParameters": None,
        "body": body, "isBase64Encoded": base64_encoded, "requestContext": {"requestId": "api-request-id"},
    }
    response = query.handler(event, Context())
    return response["statusCode"], json.loads(response["body"]) if response["body"] else None


@pytest.fixture
def raw_bucket(aws, monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
    monkeypatch.setenv("FINTRUST_RAW_BUCKET", RAW_BUCKET)
    s3 = boto3.client("s3", region_name="eu-north-1")
    s3.create_bucket(Bucket=RAW_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    return s3


@pytest.fixture
def cursor(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
    fake = MagicMock(name="cursor")

    @contextmanager
    def fake_transaction():
        yield fake

    with patch.object(db, "transaction", fake_transaction):
        yield fake


class TestUpload:
    def test_accepted_file_returns_202_and_is_stored_under_uploads(self, raw_bucket):
        status, body = call("POST", "/upload", CSV, {"filename": "october.csv"})
        assert status == 202
        assert body["status"] == "ACCEPTED"
        assert body["object_key"].startswith("uploads/") and body["object_key"].endswith("-october.csv")
        stored = raw_bucket.get_object(Bucket=RAW_BUCKET, Key=body["object_key"])
        assert stored["Body"].read().decode() == CSV
        assert stored["ContentType"] == "text/csv"

    def test_each_upload_gets_its_own_key_so_files_never_overwrite(self, raw_bucket):
        first = call("POST", "/upload", CSV, {"filename": "batch.csv"})[1]
        second = call("POST", "/upload", CSV, {"filename": "batch.csv"})[1]
        assert first["object_key"] != second["object_key"]
        assert first["upload_id"] != second["upload_id"]

    def test_default_filename_and_csv_extension(self, raw_bucket):
        assert call("POST", "/upload", CSV)[1]["object_key"].endswith("-upload.csv")
        assert call("POST", "/upload", CSV, {"filename": "batch"})[1]["object_key"].endswith("-batch.csv")

    def test_stored_key_triggers_the_ingest_lambda(self, raw_bucket):
        # The S3 trigger fires for keys ending in .csv; ingest ignores anything else.
        assert call("POST", "/upload", CSV)[1]["object_key"].endswith(".csv")

    def test_base64_encoded_body_is_decoded(self, raw_bucket):
        encoded = base64.b64encode(CSV.encode()).decode()
        status, body = call("POST", "/upload", encoded, base64_encoded=True)
        assert status == 202
        assert raw_bucket.get_object(Bucket=RAW_BUCKET, Key=body["object_key"])["Body"].read().decode() == CSV

    def test_file_with_excel_byte_order_mark_is_accepted(self, raw_bucket):
        assert call("POST", "/upload", "﻿" + CSV)[0] == 202

    @pytest.mark.parametrize(
        "filename", ["../../etc/passwd", "a/b.csv", "..", ".hidden.csv", "name with spaces.csv", "x" * 100, "a;b.csv"]
    )
    def test_unsafe_filename_is_rejected(self, raw_bucket, filename):
        status, body = call("POST", "/upload", CSV, {"filename": filename})
        assert status == 400 and "filename may contain only" in body["error"]["message"]
        assert "Contents" not in raw_bucket.list_objects_v2(Bucket=RAW_BUCKET)

    @pytest.mark.parametrize("body", [None, "", "   \n"])
    def test_empty_body_is_rejected(self, raw_bucket, body):
        status, response = call("POST", "/upload", body)
        assert status == 400 and "empty" in response["error"]["message"]

    def test_missing_header_columns_are_rejected_before_storing(self, raw_bucket):
        status, body = call("POST", "/upload", "transaction_id,amount\nTXN-1,5\n")
        assert status == 400
        assert "missing required column(s): account_id, tx_date, tx_type, channel, country_code" in body["error"]["message"]
        assert "Contents" not in raw_bucket.list_objects_v2(Bucket=RAW_BUCKET)

    def test_too_large_is_413(self, raw_bucket, monkeypatch):
        monkeypatch.setattr(query, "MAX_UPLOAD_BYTES", 50)
        status, body = call("POST", "/upload", CSV)
        assert status == 413 and body["error"]["code"] == "PAYLOAD_TOO_LARGE"

    def test_non_utf8_is_rejected(self, raw_bucket):
        encoded = base64.b64encode(b"\xff\xfe\x00not text").decode()
        status, body = call("POST", "/upload", encoded, base64_encoded=True)
        assert status == 400 and body["error"]["message"] == "The file must be UTF-8 text"

    def test_invalid_base64_is_rejected(self, raw_bucket):
        assert call("POST", "/upload", "!!!not base64!!!", base64_encoded=True)[0] == 400

    def test_storage_failure_is_503_without_internal_detail(self, raw_bucket):
        denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "secret bucket arn"}}, "PutObject")
        with patch.object(query._s3, "put_object", side_effect=denied):
            status, body = call("POST", "/upload", CSV)
        assert status == 503 and body["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "secret" not in json.dumps(body)

    def test_row_errors_are_left_to_the_ingest_pipeline(self, raw_bucket):
        """Upload only checks the header. A bad ROW is accepted here and
        quarantined by the ingest Lambda, with its reason, like any other file."""
        bad_row = ROW.replace("412.50", "-1")
        assert call("POST", "/upload", f"{HEADER}\n{bad_row}\n")[0] == 202

    def test_uploaded_file_is_processed_by_the_real_ingest_handler(self, pipeline, monkeypatch):
        from fintrust import ingest

        monkeypatch.setenv("FINTRUST_RAW_BUCKET", "fintrust-capstone-raw-test")
        key = call("POST", "/upload", CSV)[1]["object_key"]
        summary = ingest.handler(pipeline.event(key), Context())["files"][0]
        assert (summary["rows_valid"], summary["rows_queued"]) == (1, 1)
        assert pipeline.messages()[0]["body"]["source_key"] == key

    def test_s3_client_is_created_at_module_level(self):
        assert query._s3.meta.service_model.service_name == "s3"


class TestFlag:
    def test_valid_request_calls_the_database_function_with_parameters(self, cursor):
        cursor.fetchone.return_value = {"risk_event_id": 381}
        status, body = call("POST", "/flag", {"account_id": "acc-1007", "reason": "  Four high-risk payments  "})
        assert status == 201
        assert body == {"risk_event_id": 381, "account_id": "ACC-1007", "account_status": "FLAGGED"}
        cursor.execute.assert_called_once_with(
            "SELECT flag_account(%s, %s) AS risk_event_id", ("ACC-1007", "Four high-risk payments")
        )

    @pytest.mark.parametrize(
        "payload, message",
        [
            ({"reason": "x"}, "account_id must look like ACC-1234"),
            ({"account_id": "1007", "reason": "x"}, "account_id must look like ACC-1234"),
            ({"account_id": "ACC-1007"}, "reason is required"),
            ({"account_id": "ACC-1007", "reason": "   "}, "reason is required"),
            ({"account_id": "ACC-1007", "reason": 42}, "reason must be text"),
            ({"account_id": "ACC-1007", "reason": "x" * 501}, "reason must be at most 500 characters"),
        ],
    )
    def test_bad_request_is_400_and_the_database_is_not_called(self, cursor, payload, message):
        status, body = call("POST", "/flag", payload)
        assert status == 400 and body["error"]["message"] == message
        cursor.execute.assert_not_called()

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"text"', "", None])
    def test_body_must_be_a_json_object(self, cursor, raw):
        status, body = call("POST", "/flag", raw)
        assert status == 400 and "JSON" in body["error"]["message"]

    def test_personal_information_in_the_reason_is_redacted(self, cursor):
        cursor.fetchone.return_value = {"risk_event_id": 1}
        call("POST", "/flag", {"account_id": "ACC-1007", "reason": "Call customer on 082 555 0101"})
        assert cursor.execute.call_args.args[1] == ("ACC-1007", "Call customer on [REDACTED]")

    def test_unknown_account_is_404(self, cursor):
        cursor.execute.side_effect = db.DatabaseError("Account ACC-0000 does not exist", "P0002")
        status, body = call("POST", "/flag", {"account_id": "ACC-0000", "reason": "x"})
        assert status == 404 and body["error"]["code"] == "NOT_FOUND"

    def test_closed_account_is_409(self, cursor):
        cursor.execute.side_effect = db.DatabaseError("closed", "55000")
        status, body = call("POST", "/flag", {"account_id": "ACC-1012", "reason": "x"})
        assert status == 409 and body["error"]["code"] == "CONFLICT"

    def test_other_database_error_is_a_generic_500(self, cursor):
        cursor.execute.side_effect = db.DatabaseError("relation risk_events is broken", "XX000")
        status, body = call("POST", "/flag", {"account_id": "ACC-1007", "reason": "x"})
        assert status == 500 and "risk_events" not in json.dumps(body)

    def test_flag_is_logged_with_a_masked_account(self, cursor, capsys):
        cursor.fetchone.return_value = {"risk_event_id": 381}
        call("POST", "/flag", {"account_id": "ACC-1007", "reason": "Sensitive reason text"})
        output = capsys.readouterr().out
        assert "account_flagged" in output and "ACC-**07" in output
        assert "ACC-1007" not in output and "Sensitive reason text" not in output


class TestResolve:
    def test_open_alert_is_resolved(self, cursor):
        cursor.fetchone.return_value = {"alert_id": 57, "status": "RESOLVED", "resolved_at": "2026-10-07T08:00:00Z",
                                        "resolved_by": "n.hlongwane"}
        status, body = call("POST", "/resolve", {"alert_id": 57, "resolved_by": "n.hlongwane",
                                                 "resolution_note": "Confirmed with customer"})
        assert status == 200 and body["status"] == "RESOLVED" and body["alert_id"] == 57
        sql, values = cursor.execute.call_args.args
        assert "status = 'OPEN'" in sql                      # only an open alert can be resolved
        assert values == ("n.hlongwane", "Confirmed with customer", 57)

    def test_note_is_optional(self, cursor):
        cursor.fetchone.return_value = {"alert_id": 57, "status": "RESOLVED"}
        assert call("POST", "/resolve", {"alert_id": 57, "resolved_by": "n.hlongwane"})[0] == 200
        assert cursor.execute.call_args.args[1] == ("n.hlongwane", None, 57)

    def test_unknown_alert_is_404(self, cursor):
        cursor.fetchone.side_effect = [None, None]
        status, body = call("POST", "/resolve", {"alert_id": 9999, "resolved_by": "n.hlongwane"})
        assert status == 404 and body["error"]["message"] == "No alert with id 9999"

    def test_already_resolved_alert_is_409(self, cursor):
        cursor.fetchone.side_effect = [None, {"status": "RESOLVED"}]
        status, body = call("POST", "/resolve", {"alert_id": 57, "resolved_by": "n.hlongwane"})
        assert status == 409 and body["error"]["code"] == "CONFLICT"

    @pytest.mark.parametrize(
        "payload, fragment",
        [
            ({"resolved_by": "n.hlongwane"}, "alert_id must be a positive whole number"),
            ({"alert_id": "57", "resolved_by": "n.hlongwane"}, "alert_id must be a positive whole number"),
            ({"alert_id": 0, "resolved_by": "n.hlongwane"}, "alert_id must be a positive whole number"),
            ({"alert_id": True, "resolved_by": "n.hlongwane"}, "alert_id must be a positive whole number"),
            ({"alert_id": 5.5, "resolved_by": "n.hlongwane"}, "alert_id must be a positive whole number"),
            ({"alert_id": 57}, "resolved_by must be"),
            ({"alert_id": 57, "resolved_by": "x"}, "resolved_by must be"),
            ({"alert_id": 57, "resolved_by": "Robert'); DROP TABLE alerts;--"}, "resolved_by must be"),
            ({"alert_id": 57, "resolved_by": "n.hlongwane", "resolution_note": "x" * 501}, "at most 500 characters"),
        ],
    )
    def test_bad_request_is_400_and_the_database_is_not_called(self, cursor, payload, fragment):
        status, body = call("POST", "/resolve", payload)
        assert status == 400 and fragment in body["error"]["message"]
        cursor.execute.assert_not_called()


class TestAlertsValidation:
    def test_bad_status_is_400(self, cursor):
        status, body = call("GET", "/alerts", params={"status": "PENDING"})
        assert status == 400 and body["error"]["message"] == "status must be one of OPEN, RESOLVED"
        cursor.execute.assert_not_called()

    def test_status_is_passed_as_a_parameter(self, cursor):
        cursor.fetchone.return_value = {"total": 0}
        cursor.fetchall.return_value = []
        assert call("GET", "/alerts", params={"status": "open", "limit": "10"})[0] == 200
        sql, values = cursor.execute.call_args_list[1].args
        assert "a.status = %s" in sql and "OPEN" not in sql
        assert values == ["OPEN", 10, 0]


class TestRoutesMatchTheApiDefinition:
    """The routes the Lambda handles and the routes API Gateway exposes must be
    the same list, or a route would exist that can never be called (or the reverse)."""

    def test_every_lambda_route_is_in_openapi_and_the_reverse(self):
        definition = json.loads(
            (Path(__file__).resolve().parents[2] / "infrastructure" / "apigateway" / "openapi.json").read_text()
        )
        published = {
            (method.upper(), path)
            for path, methods in definition["paths"].items()
            for method in methods
            if method != "options"
        }
        assert published == set(query.ROUTES)

    def test_every_published_route_requires_the_api_key_and_has_a_preflight(self):
        definition = json.loads(
            (Path(__file__).resolve().parents[2] / "infrastructure" / "apigateway" / "openapi.json").read_text()
        )
        for path, methods in definition["paths"].items():
            assert "options" in methods, f"{path} has no OPTIONS method for browsers"
            assert "security" not in methods["options"]
            for method, operation in methods.items():
                if method != "options":
                    assert operation["security"] == [{"api_key": []}], f"{method} {path} is not protected"
