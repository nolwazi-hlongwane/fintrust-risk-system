"""Unit tests for the query Lambda: routing, validation and response format,
with the database stubbed. The SQL is tested against a real PostgreSQL in
tests/integration/test_query_integration.py."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from fintrust import db, dynamo, query
from fintrust.models import RiskScore, RuleHit
from tests.conftest import signed_in


class Context:
    aws_request_id = "lambda-request-id"


def api_event(method="GET", resource="/health", params=None, path=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "queryStringParameters": params,
        "pathParameters": path,
        "requestContext": signed_in("api-request-id"),
    }


def call(method="GET", resource="/health", params=None, path=None):
    response = query.handler(api_event(method, resource, params, path), Context())
    body = json.loads(response["body"]) if response["body"] else None
    return response["statusCode"], body, response


@pytest.fixture(autouse=True)
def no_ssm(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")


@pytest.fixture
def cursor():
    """A fake database cursor; db.transaction() yields it."""
    fake = MagicMock(name="cursor")
    fake.fetchone.return_value = {"total": 0}
    fake.fetchall.return_value = []

    @contextmanager
    def fake_transaction():
        yield fake

    with patch.object(db, "transaction", fake_transaction):
        yield fake


class TestRoutingAndFormat:
    def test_unknown_route_is_404(self):
        status, body, _ = call("GET", "/nope")
        assert status == 404
        assert body["error"]["code"] == "NOT_FOUND"

    def test_wrong_method_is_404(self):
        assert call("DELETE", "/transactions")[0] == 404

    def test_every_response_is_json_with_security_headers(self):
        _, _, response = call("GET", "/nope")
        headers = response["headers"]
        assert headers["Content-Type"] == "application/json"
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "x-api-key" in headers["Access-Control-Allow-Headers"]

    def test_error_carries_the_api_gateway_request_id(self):
        assert call("GET", "/nope")[1]["error"]["request_id"] == "api-request-id"

    def test_preflight_request_gets_cors_headers_and_no_body(self):
        status, body, response = call("OPTIONS", "/transactions")
        assert status == 204 and body is None
        assert response["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_allowed_origin_comes_from_configuration(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", "https://d123.cloudfront.net")
        assert call("OPTIONS", "/health")[2]["headers"]["Access-Control-Allow-Origin"] == "https://d123.cloudfront.net"

    def test_unexpected_error_is_a_generic_500_and_logged(self, capsys):
        with patch.dict(query.ROUTES, {("GET", "/health"): MagicMock(side_effect=RuntimeError("secret detail"))}):
            status, body, _ = call("GET", "/health")
        assert status == 500
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert "secret detail" not in json.dumps(body)
        assert "secret detail" in capsys.readouterr().out

    def test_database_error_is_a_generic_500(self):
        with patch.dict(query.ROUTES, {("GET", "/insights"): MagicMock(side_effect=db.DatabaseError("syntax error at"))}):
            status, body, _ = call("GET", "/insights")
        assert status == 500 and "syntax" not in json.dumps(body)

    def test_database_unavailable_is_503(self):
        with patch.dict(query.ROUTES, {("GET", "/insights"): MagicMock(side_effect=db.DatabaseUnavailable("down"))}):
            status, body, _ = call("GET", "/insights")
        assert status == 503 and body["error"]["code"] == "SERVICE_UNAVAILABLE"

    def test_request_is_logged_with_masked_account(self, cursor, capsys):
        call("GET", "/transactions", {"account_id": "ACC-1007"})
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert line["message"] == "request_completed"
        assert (line["method"], line["resource"], line["status"]) == ("GET", "/transactions", 200)
        assert line["account"] == "ACC-**07"
        assert "duration_ms" in line


class TestHealth:
    def test_healthy(self):
        with patch.object(db, "is_healthy", return_value=True):
            status, body, _ = call("GET", "/health")
        assert status == 200
        assert body == {"status": "ok", "database": "ok", "version": "0.1.0"}

    def test_database_down(self):
        with patch.object(db, "is_healthy", return_value=False):
            status, body, _ = call("GET", "/health")
        assert status == 503 and body["database"] == "unavailable"


class TestTransactionsValidation:
    @pytest.mark.parametrize(
        "params, message",
        [
            ({"account_id": "AC-1007"}, "account_id must look like ACC-1234"),
            ({"account_id": "ACC-1007' OR '1'='1"}, "account_id must look like ACC-1234"),
            ({"transaction_id": "x; DROP TABLE"}, "transaction_id is not in a valid format"),
            ({"risk_level": "CRITICAL"}, "risk_level must be one of LOW, MEDIUM, HIGH"),
            ({"from": "01/10/2026"}, "from must be a date in the form YYYY-MM-DD"),
            ({"to": "2026-02-30"}, "to must be a date in the form YYYY-MM-DD"),
            ({"from": "2026-10-05", "to": "2026-10-01"}, "from must not be after to"),
            ({"limit": "0"}, "limit must be between 1 and 100"),
            ({"limit": "101"}, "limit must be between 1 and 100"),
            ({"limit": "ten"}, "limit must be a whole number"),
            ({"offset": "-1"}, "offset must be between 0 and 1000000"),
        ],
    )
    def test_bad_parameter_is_400_and_no_query_runs(self, cursor, params, message):
        status, body, _ = call("GET", "/transactions", params)
        assert status == 400
        assert body["error"] == {"code": "VALIDATION_ERROR", "message": message, "request_id": "api-request-id"}
        cursor.execute.assert_not_called()

    def test_no_parameters_uses_defaults(self, cursor):
        status, body, _ = call("GET", "/transactions")
        assert status == 200
        assert body == {"items": [], "sort": "newest",
                        "pagination": {"limit": 25, "offset": 0, "total": 0, "has_more": False}}

    def test_values_are_passed_as_parameters_never_in_the_sql_text(self, cursor):
        call("GET", "/transactions", {"account_id": "acc-1007", "risk_level": "high", "limit": "10", "offset": "20"})
        sql, values = cursor.execute.call_args_list[1].args
        assert "ACC-1007" not in sql and "HIGH" not in sql
        assert "account_id = %s" in sql and "risk_level = %s" in sql
        assert values == ["ACC-1007", "HIGH", 10, 20]

    def test_to_date_is_inclusive(self, cursor):
        call("GET", "/transactions", {"from": "2026-09-01", "to": "2026-09-30"})
        _sql, values = cursor.execute.call_args_list[0].args
        assert values[0].isoformat() == "2026-09-01T00:00:00+02:00"
        assert values[1].isoformat() == "2026-10-01T00:00:00+02:00"

    def test_amounts_and_dates_are_serialised_without_losing_precision(self, cursor):
        cursor.fetchone.return_value = {"total": 1}
        cursor.fetchall.return_value = [{
            "transaction_id": "TXN-1", "amount": Decimal("104500.10"),
            "tx_date": datetime(2026, 9, 15, 13, 10, tzinfo=timezone.utc), "risk_score": 70,
        }]
        _, body, _ = call("GET", "/transactions")
        assert body["items"][0]["amount"] == "104500.10"
        assert body["items"][0]["tx_date"].startswith("2026-09-15 13:10:00")
        assert body["items"][0]["risk_score"] == 70

    def test_has_more(self, cursor):
        cursor.fetchone.return_value = {"total": 60}
        cursor.fetchall.return_value = [{"transaction_id": f"T{n}"} for n in range(25)]
        assert call("GET", "/transactions")[1]["pagination"]["has_more"] is True
        assert call("GET", "/transactions", {"offset": "50"})[1]["pagination"]["has_more"] is False


class TestJourney:
    TXN = "TXN-20260915-000231"
    STAMP = "2026-09-15T13:10:00Z"

    def path(self):
        return {"transaction_id": self.TXN}

    def test_scored_transaction(self, events_table):
        for stage in ("RECEIVED", "VALIDATED", "QUEUED"):
            dynamo.put_event("ACC-1007", self.STAMP, self.TXN, stage)
        dynamo.put_score(RiskScore(self.TXN, "ACC-1007", self.STAMP, 70, "HIGH", (
            RuleHit("HIGH_AMOUNT", 50, "Amount 104500.00 is above 50000"),
            RuleHit("CROSS_BORDER", 20, "Country AE is not ZA"),
        )))
        dynamo.put_event("ACC-1007", self.STAMP, self.TXN, "SCORED", "Score 70 (HIGH)")
        status, body, _ = call("GET", "/transactions/{transaction_id}/journey", path=self.path())
        assert status == 200
        assert body["status"] == "SCORED"
        assert body["account_id"] == "ACC-1007"
        assert body["score"]["risk_score"] == 70
        assert body["score"]["rules"][0] == {"rule": "HIGH_AMOUNT", "points": 50,
                                             "detail": "Amount 104500.00 is above 50000"}
        assert [event["stage"] for event in body["events"]] == ["RECEIVED", "VALIDATED", "QUEUED", "SCORED"]
        assert "detail" not in body["events"][1]

    @pytest.mark.parametrize("failures, expected", [(0, "QUEUED"), (1, "RETRYING"), (2, "RETRYING"), (3, "DEAD_LETTERED")])
    def test_status_of_a_transaction_that_has_not_been_scored(self, events_table, failures, expected):
        dynamo.put_event("ACC-9999", self.STAMP, self.TXN, "QUEUED")
        for attempt in range(failures):
            dynamo.put_event("ACC-9999", self.STAMP, self.TXN, "SCORE_FAILED", f"Attempt {attempt + 1} of 3")
        _, body, _ = call("GET", "/transactions/{transaction_id}/journey", path=self.path())
        assert body["status"] == expected
        assert body["score"] is None

    def test_status_after_recovery_and_alert(self, events_table):
        for _ in range(3):
            dynamo.put_event("ACC-1007", self.STAMP, self.TXN, "SCORE_FAILED")
        dynamo.put_score(RiskScore(self.TXN, "ACC-1007", self.STAMP, 70, "HIGH"))
        assert call("GET", "/transactions/{transaction_id}/journey", path=self.path())[1]["status"] == "SCORED"
        dynamo.put_event("ACC-1007", self.STAMP, self.TXN, "ALERT_SENT")
        assert call("GET", "/transactions/{transaction_id}/journey", path=self.path())[1]["status"] == "ALERTED"

    def test_unknown_transaction_is_404(self, events_table, cursor):
        cursor.fetchone.return_value = None           # not in the ledger either
        status, body, _ = call("GET", "/transactions/{transaction_id}/journey", path={"transaction_id": "TXN-NOPE-000001"})
        assert status == 404 and body["error"]["code"] == "NOT_FOUND"

    def test_transaction_loaded_as_opening_data_is_explained_from_the_ledger(self, events_table, cursor):
        """No journey in DynamoDB, but RDS has the score and the rules that fired."""
        cursor.fetchone.return_value = {"account_id": "ACC-1007", "risk_score": 70, "risk_level": "HIGH",
                                        "created_at": "2026-09-15 13:15:04+00:00"}
        cursor.fetchall.return_value = [
            {"event_type": "HIGH_AMOUNT", "points": 50, "reason": "Amount 104500.00 is above 50000"},
            {"event_type": "CROSS_BORDER", "points": 20, "reason": "Country AE is not ZA"},
        ]
        status, body, _ = call("GET", "/transactions/{transaction_id}/journey", path=self.path())
        assert status == 200
        assert (body["source"], body["status"], body["events"]) == ("ledger", "SCORED", [])
        assert body["score"]["risk_score"] == 70
        assert body["score"]["rules"][0] == {"rule": "HIGH_AMOUNT", "points": 50,
                                             "detail": "Amount 104500.00 is above 50000"}
        assert cursor.execute.call_args_list[0].args[1] == (self.TXN,)      # the id is a parameter, not SQL text

    def test_ledger_is_not_consulted_when_the_event_store_has_the_journey(self, events_table, cursor):
        dynamo.put_event("ACC-1007", self.STAMP, self.TXN, "QUEUED")
        _, body, _ = call("GET", "/transactions/{transaction_id}/journey", path=self.path())
        assert body["source"] == "event_store"
        cursor.execute.assert_not_called()

    @pytest.mark.parametrize("value", ["", "x", "TXN 1 2 3 4", "../../etc/passwd"])
    def test_bad_id_is_400(self, events_table, value):
        assert call("GET", "/transactions/{transaction_id}/journey", path={"transaction_id": value})[0] == 400


class TestInsightsValidation:
    def test_bad_date_is_400(self, cursor):
        assert call("GET", "/insights", {"from": "soon"})[0] == 400
        cursor.execute.assert_not_called()

    def test_shape_with_no_data(self, cursor):
        cursor.fetchone.side_effect = [
            {"transactions": 0, "total_value": Decimal("0"), "accounts": 0, "low": 0, "medium": 0, "high": 0},
            {"open_alerts": 0},
        ]
        status, body, _ = call("GET", "/insights", {"from": "2026-07-01", "to": "2026-09-30"})
        assert status == 200
        assert body == {
            "period": {"from": "2026-07-01", "to": "2026-09-30"},
            "totals": {"transactions": 0, "total_value": "0", "accounts": 0},
            "by_risk_level": {"LOW": 0, "MEDIUM": 0, "HIGH": 0},
            "rules_fired": {"HIGH_AMOUNT": 0, "HIGH_VELOCITY": 0, "CROSS_BORDER": 0},
            "open_alerts": 0,
            "top_risk_accounts": [],
        }
