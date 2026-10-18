"""Unit tests: who may call the API (Week 15 Day 2).

API Gateway verifies the Cognito ID token and passes its claims to the Lambda.
These tests check what the Lambda then does with them: no identity, no session,
the wrong role, and the full sign in / use / sign out cycle against a fake
DynamoDB session table.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest

from fintrust import db, query, sessions
from tests.conftest import ANALYST_CLAIMS, OFFICER_CLAIMS, REGION

ROOT = Path(__file__).resolve().parents[2]


class Context:
    aws_request_id = "lambda-request-id"


def call(method, resource, claims=OFFICER_CLAIMS, body=None, params=None, headers=None):
    context = {"requestId": "api-request-id"}
    if claims is not None:
        context["authorizer"] = {"claims": dict(claims)}
    response = query.handler({
        "httpMethod": method, "resource": resource, "queryStringParameters": params, "pathParameters": None,
        "headers": headers or {}, "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False, "requestContext": context,
    }, Context())
    return response["statusCode"], json.loads(response["body"]) if response["body"] else None


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
    monkeypatch.setenv("FINTRUST_CORS_ALLOWED_ORIGIN", "https://dashboard.example")


@pytest.fixture
def cursor(monkeypatch):
    fake = MagicMock()
    fake.fetchone.return_value = {"total": 0, "transactions": 0, "total_value": 0, "accounts": 0, "low": 0,
                                  "medium": 0, "high": 0, "open_alerts": 0, "status": "OPEN"}
    fake.fetchall.return_value = []

    @contextmanager
    def transaction():
        yield fake

    monkeypatch.setattr(db, "transaction", transaction)
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: {"risk_event_id": 1})
    monkeypatch.setattr(db, "is_healthy", lambda: True)
    return fake


class TestIdentityIsRequired:
    def test_health_needs_no_sign_in(self, cursor):
        assert call("GET", "/health", claims=None)[0] == 200

    @pytest.mark.parametrize("method, resource", [
        ("GET", "/transactions"), ("GET", "/insights"), ("GET", "/alerts"), ("GET", "/accounts"),
        ("POST", "/upload"), ("POST", "/flag"), ("POST", "/resolve"), ("GET", "/session"), ("POST", "/session"),
    ])
    def test_every_other_route_is_refused_without_a_sign_in(self, cursor, method, resource):
        """Defence in depth: even if the authorizer were missing from API Gateway, nothing opens."""
        status, body = call(method, resource, claims=None)
        assert status == 401 and body["error"]["code"] == "NOT_SIGNED_IN"
        cursor.execute.assert_not_called()

    def test_a_token_without_a_session_identifier_is_refused(self, cursor):
        claims = {key: value for key, value in OFFICER_CLAIMS.items() if key != "origin_jti"}
        assert call("GET", "/transactions", claims=claims)[0] == 401

    def test_a_strange_username_is_refused(self, cursor):
        status, body = call("GET", "/transactions", claims={**OFFICER_CLAIMS, "cognito:username": "bad name;--"})
        assert status == 403 and body["error"]["code"] == "FORBIDDEN"


class TestRoles:
    @pytest.mark.parametrize("resource", ["/transactions", "/insights", "/alerts", "/accounts"])
    def test_an_analyst_can_read(self, cursor, resource):
        assert call("GET", resource, claims=ANALYST_CLAIMS)[0] == 200

    @pytest.mark.parametrize("resource, body", [
        ("/resolve", {"alert_id": 57}),
        ("/flag", {"account_id": "ACC-1007", "reason": "x"}),
    ])
    def test_an_analyst_cannot_change_the_record(self, cursor, resource, body):
        status, answer = call("POST", resource, claims=ANALYST_CLAIMS, body=body)
        assert status == 403 and answer["error"]["code"] == "FORBIDDEN"
        assert "compliance officer" in answer["error"]["message"]
        cursor.execute.assert_not_called()

    def test_an_officer_can_resolve(self, cursor):
        cursor.fetchone.return_value = {"alert_id": 57, "status": "RESOLVED"}
        assert call("POST", "/resolve", body={"alert_id": 57})[0] == 200

    def test_a_person_with_no_role_can_do_nothing(self, cursor):
        claims = {**OFFICER_CLAIMS, "cognito:groups": ""}
        status, body = call("GET", "/transactions", claims=claims)
        assert status == 403 and body["error"]["code"] == "NO_ROLE"

    def test_an_unknown_group_gives_no_role(self, cursor):
        claims = {**OFFICER_CLAIMS, "cognito:groups": "administrators"}
        assert call("GET", "/transactions", claims=claims)[1]["error"]["code"] == "NO_ROLE"

    @pytest.mark.parametrize("groups", [
        "compliance-officer", "compliance-analyst,compliance-officer", "[compliance-analyst compliance-officer]",
        ["compliance-officer"],
    ])
    def test_groups_are_read_in_every_format_api_gateway_uses(self, groups):
        caller = query.caller_from({"requestContext": {"authorizer": {"claims": {**OFFICER_CLAIMS,
                                                                                  "cognito:groups": groups}}}})
        assert caller.is_officer

    def test_username_is_read_in_lower_case(self):
        caller = query.caller_from({"requestContext": {"authorizer": {"claims": {**OFFICER_CLAIMS,
                                                                                  "cognito:username": "T.Nkosi"}}}})
        assert caller.username == "t.nkosi"


class TestSessionIsChecked:
    @pytest.fixture
    def ended(self, monkeypatch):
        def refuse(session_id, username):
            raise sessions.SessionEnded(sessions.IDLE)

        monkeypatch.setattr(sessions, "require_active", refuse)

    def test_an_ended_session_is_401_with_the_reason(self, cursor, ended):
        status, body = call("GET", "/transactions")
        assert status == 401
        assert body["error"]["code"] == "SESSION_ENDED" and body["error"]["reason"] == "IDLE"
        assert "without activity" in body["error"]["message"]
        cursor.execute.assert_not_called()

    def test_role_is_checked_before_the_session(self, cursor, ended):
        """An analyst asking to resolve is told about the role, which is the real reason."""
        assert call("POST", "/resolve", claims=ANALYST_CLAIMS, body={"alert_id": 1})[1]["error"]["code"] == "FORBIDDEN"

    def test_the_session_store_being_down_is_503_not_500(self, cursor, monkeypatch):
        def broken(session_id, username):
            raise sessions.SessionError("down")

        monkeypatch.setattr(sessions, "require_active", broken)
        status, body = call("GET", "/transactions")
        assert status == 503 and body["error"]["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.real_sessions
class TestSignInUseSignOut:
    """The whole cycle against the real session code and a fake DynamoDB table."""

    @pytest.fixture(autouse=True)
    def just_signed_in(self, monkeypatch):
        """The tokens say auth_time 1791000000; the clock reads a minute later."""
        monkeypatch.setattr(sessions, "_now", lambda: 1_791_000_060)

    def test_a_sign_in_older_than_the_session_limit_cannot_start(self, cursor, sessions_table, monkeypatch):
        monkeypatch.setattr(sessions, "_now", lambda: 1_791_000_000 + 9 * 3600)
        status, body = call("POST", "/session")
        assert status == 401 and body["error"]["reason"] == "EXPIRED"

    def test_sign_in_use_sign_out_and_the_token_stops_working(self, cursor, sessions_table):
        status, body = call("POST", "/session", headers={"User-Agent": "Test browser"})
        assert status == 201
        assert body["username"] == "t.nkosi" and body["name"] == "Thabo Nkosi"
        assert body["roles"] == ["compliance-officer"]
        assert body["permissions"] == {"read": True, "upload": True, "resolve_alerts": True, "flag_accounts": True}

        assert call("POST", "/session")[0] == 200                     # asking again is harmless
        assert call("GET", "/transactions")[0] == 200
        assert call("GET", "/session")[1]["status"] == "ACTIVE"

        assert call("DELETE", "/session") == (200, {"status": "ENDED", "end_reason": "SIGNED_OUT"})

        # The ID token itself has not expired, but the API no longer accepts it.
        status, body = call("GET", "/transactions")
        assert status == 401 and body["error"]["reason"] == "SIGNED_OUT"
        status, body = call("POST", "/session")                       # and it cannot reopen the session
        assert status == 401 and body["error"]["code"] == "SESSION_ENDED"

    def test_a_request_before_the_session_is_started_is_refused(self, cursor, sessions_table):
        status, body = call("GET", "/transactions")
        assert status == 401 and body["error"]["reason"] == "NOT_STARTED"

    def test_an_analyst_session_has_read_only_permissions(self, cursor, sessions_table):
        body = call("POST", "/session", claims=ANALYST_CLAIMS)[1]
        assert body["permissions"] == {"read": True, "upload": True, "resolve_alerts": False, "flag_accounts": False}

    def test_no_session_is_started_for_a_person_without_a_role(self, cursor, sessions_table):
        status, body = call("POST", "/session", claims={**OFFICER_CLAIMS, "cognito:groups": ""})
        assert status == 403 and body["error"]["code"] == "NO_ROLE"
        assert sessions_table.scan()["Count"] == 0

    def test_signing_out_twice_is_harmless(self, cursor, sessions_table):
        call("POST", "/session")
        assert call("DELETE", "/session")[0] == 200
        assert call("DELETE", "/session")[0] == 200

    def test_one_persons_sign_out_does_not_end_another_persons_session(self, cursor, sessions_table):
        call("POST", "/session")
        call("POST", "/session", claims=ANALYST_CLAIMS)
        call("DELETE", "/session")
        assert call("GET", "/transactions", claims=ANALYST_CLAIMS)[0] == 200


class TestAuditTrail:
    def test_every_log_line_of_a_request_says_who_made_it(self, cursor, capsys):
        call("GET", "/transactions")
        completed = [json.loads(line) for line in capsys.readouterr().out.splitlines()
                     if '"request_completed"' in line]
        assert completed[-1]["actor"] == "t.nkosi"

    def test_tokens_are_never_logged(self, cursor, capsys):
        call("GET", "/transactions", headers={"Authorization": "eyJraWQiOiJzZWNyZXQifQ.payload.signature"})
        output = capsys.readouterr().out
        assert "eyJ" not in output and "officer-session-0001" not in output

    def test_the_uploader_is_stored_with_the_file(self, aws, monkeypatch):
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="raw-test", CreateBucketConfiguration={"LocationConstraint": REGION})
        monkeypatch.setenv("FINTRUST_RAW_BUCKET", "raw-test")
        header = (ROOT / "data" / "sample_transactions_valid.csv").read_text().splitlines()[0]
        with patch.object(query, "_s3", s3):
            response = query.handler({
                "httpMethod": "POST", "resource": "/upload", "queryStringParameters": {"filename": "a.csv"},
                "pathParameters": None, "headers": {}, "body": header + "\n", "isBase64Encoded": False,
                "requestContext": {"requestId": "r", "authorizer": {"claims": dict(ANALYST_CLAIMS)}},
            }, Context())
        assert response["statusCode"] == 202
        key = json.loads(response["body"])["object_key"]
        assert s3.head_object(Bucket="raw-test", Key=key)["Metadata"] == {"uploaded-by": "l.dube"}


class TestCors:
    def test_the_browser_may_send_the_authorization_header_and_delete(self, cursor):
        response = query.handler({"httpMethod": "OPTIONS", "resource": "/session", "headers": {},
                                  "requestContext": {"requestId": "r"}}, Context())
        assert "authorization" in response["headers"]["Access-Control-Allow-Headers"]
        assert "DELETE" in response["headers"]["Access-Control-Allow-Methods"]
