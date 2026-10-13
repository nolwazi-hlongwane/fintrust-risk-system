"""Integration tests: the flag_account stored function, and the /flag, /resolve
and /alerts routes, against a real PostgreSQL as the fintrust_query user."""

import json

import pytest
from psycopg2 import errors

from fintrust import db, query
from fintrust.db import DatabaseError
from tests.conftest import OFFICER_CLAIMS, signed_in

pytestmark = pytest.mark.integration


class Context:
    aws_request_id = "test-request-id"


SECOND_OFFICER = {**OFFICER_CLAIMS, "cognito:username": "s.pillay", "origin_jti": "second-officer-session"}


def call(method, resource, body=None, params=None, claims=None):
    response = query.handler(
        {"httpMethod": method, "resource": resource, "queryStringParameters": params, "pathParameters": None,
         "body": json.dumps(body) if body is not None else None, "isBase64Encoded": False,
         "requestContext": signed_in("api-request-id", claims)},
        Context(),
    )
    return response["statusCode"], json.loads(response["body"])


def rows(admin, sql, params=None):
    with admin.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


@pytest.fixture
def ledger(as_db_user, admin):
    """Connect as fintrust_query, and undo anything a test changes."""
    as_db_user("fintrust_query")
    before = dict(rows(admin, "SELECT account_id, status FROM accounts"))
    last_event = rows(admin, "SELECT COALESCE(max(risk_event_id), 0) FROM risk_events")[0][0]
    resolved_before = {row[0] for row in rows(admin, "SELECT alert_id FROM alerts WHERE status = 'RESOLVED'")}
    yield admin
    with admin.cursor() as cursor:
        cursor.execute("DELETE FROM risk_events WHERE risk_event_id > %s", (last_event,))
        for account_id, status in before.items():
            cursor.execute("UPDATE accounts SET status = %s WHERE account_id = %s", (status, account_id))
        cursor.execute(
            "UPDATE alerts SET status = 'OPEN', resolved_at = NULL, resolved_by = NULL, resolution_note = NULL "
            "WHERE status = 'RESOLVED' AND NOT (alert_id = ANY(%s))", (list(resolved_before),))


class TestFlagAccountFunction:
    """sql/functions.sql, called directly as the admin user."""

    def test_inserts_the_event_and_updates_the_status_together(self, ledger):
        event_id = rows(ledger, "SELECT flag_account('ACC-1001', 'Unusual pattern', 't.nkosi')")[0][0]
        assert rows(ledger, "SELECT status FROM accounts WHERE account_id = 'ACC-1001'") == [("FLAGGED",)]
        assert rows(ledger, "SELECT account_id, transaction_id, event_type, points, reason, flagged_by FROM risk_events "
                            "WHERE risk_event_id = %s", (event_id,)) == [
            ("ACC-1001", None, "MANUAL_FLAG", 0, "Unusual pattern", "t.nkosi")]

    @pytest.mark.parametrize("who", ["NULL", "''", "'   '"])
    def test_the_person_flagging_is_required(self, ledger, who):
        with pytest.raises(errors.CheckViolation, match="name of the person flagging"):
            rows(ledger, f"SELECT flag_account('ACC-1001', 'Unusual pattern', {who})")
        assert rows(ledger, "SELECT status FROM accounts WHERE account_id = 'ACC-1001'") == [("ACTIVE",)]

    def test_the_old_two_argument_version_no_longer_exists(self, ledger):
        with pytest.raises(errors.UndefinedFunction):
            rows(ledger, "SELECT flag_account('ACC-1001', 'no name recorded')")

    def test_only_a_manual_flag_may_name_a_person(self, ledger):
        with pytest.raises(errors.CheckViolation, match="ck_risk_events_flagged_by"):
            with ledger.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO risk_events (account_id, transaction_id, event_type, points, reason, flagged_by) "
                    "SELECT t.account_id, t.transaction_id, 'HIGH_VELOCITY', 30, 'x', 't.nkosi' FROM transactions t "
                    "WHERE NOT EXISTS (SELECT 1 FROM risk_events e WHERE e.transaction_id = t.transaction_id "
                    "AND e.event_type = 'HIGH_VELOCITY') LIMIT 1")

    def test_unknown_account_raises_and_changes_nothing(self, ledger):
        before = rows(ledger, "SELECT count(*) FROM risk_events")
        with pytest.raises(errors.NoDataFound, match="Account ACC-0000 does not exist"):
            rows(ledger, "SELECT flag_account('ACC-0000', 'x', 't.nkosi')")
        assert rows(ledger, "SELECT count(*) FROM risk_events") == before

    def test_closed_account_cannot_be_flagged(self, ledger):
        with pytest.raises(errors.ObjectNotInPrerequisiteState, match="is closed"):
            rows(ledger, "SELECT flag_account('ACC-1012', 'x', 't.nkosi')")
        assert rows(ledger, "SELECT status FROM accounts WHERE account_id = 'ACC-1012'") == [("CLOSED",)]

    @pytest.mark.parametrize("reason", ["NULL", "''", "'   '"])
    def test_a_reason_is_required(self, ledger, reason):
        with pytest.raises(errors.CheckViolation, match="reason is required"):
            rows(ledger, f"SELECT flag_account('ACC-1001', {reason}, 't.nkosi')")
        assert rows(ledger, "SELECT status FROM accounts WHERE account_id = 'ACC-1001'") == [("ACTIVE",)]

    def test_is_atomic_if_the_status_update_fails_the_event_is_not_kept(self, ledger):
        """Make the UPDATE fail after the INSERT has run, and check the INSERT was undone."""
        before = rows(ledger, "SELECT count(*) FROM risk_events")
        with ledger.cursor() as cursor:
            cursor.execute("ALTER TABLE accounts ADD CONSTRAINT test_no_flag CHECK (status <> 'FLAGGED') NOT VALID")
        try:
            with pytest.raises(errors.CheckViolation):
                rows(ledger, "SELECT flag_account('ACC-1001', 'will fail part-way', 't.nkosi')")
            assert rows(ledger, "SELECT count(*) FROM risk_events") == before
            assert rows(ledger, "SELECT status FROM accounts WHERE account_id = 'ACC-1001'") == [("ACTIVE",)]
        finally:
            with ledger.cursor() as cursor:
                cursor.execute("ALTER TABLE accounts DROP CONSTRAINT test_no_flag")

    def test_flagging_twice_keeps_both_reasons(self, ledger):
        rows(ledger, "SELECT flag_account('ACC-1001', 'first', 't.nkosi')")
        rows(ledger, "SELECT flag_account('ACC-1001', 'second', 'l.dube')")
        reasons = rows(ledger, "SELECT reason, flagged_by FROM risk_events WHERE account_id = 'ACC-1001' "
                               "AND event_type = 'MANUAL_FLAG' ORDER BY risk_event_id")
        assert reasons == [("first", "t.nkosi"), ("second", "l.dube")]

    def test_the_public_cannot_execute_it(self, ledger):
        acl = rows(ledger, "SELECT proacl::text FROM pg_proc WHERE proname = 'flag_account'")[0][0]
        assert "fintrust_query=X" in acl
        assert "=X/" not in acl.replace("fintrust_query=X/", "").replace("postgres=X/", "")

    def test_runs_with_a_fixed_search_path(self, ledger):
        config = rows(ledger, "SELECT proconfig, prosecdef FROM pg_proc WHERE proname = 'flag_account'")[0]
        assert config == (["search_path=public, pg_temp"], True)


class TestFlagRoute:
    def test_flag_changes_the_ledger(self, ledger):
        status, body = call("POST", "/flag", {"account_id": "ACC-1004", "reason": "Three large payments in a week"})
        assert status == 201
        assert body["account_id"] == "ACC-1004" and body["account_status"] == "FLAGGED"
        assert rows(ledger, "SELECT status FROM accounts WHERE account_id = 'ACC-1004'") == [("FLAGGED",)]
        assert body["flagged_by"] == "t.nkosi"
        assert rows(ledger, "SELECT reason, flagged_by FROM risk_events WHERE risk_event_id = %s",
                    (body["risk_event_id"],)) == [("Three large payments in a week", "t.nkosi")]

    def test_unknown_account_is_404(self, ledger):
        status, body = call("POST", "/flag", {"account_id": "ACC-0000", "reason": "x"})
        assert status == 404 and body["error"]["message"] == "Account ACC-0000 does not exist"

    def test_closed_account_is_409(self, ledger):
        status, body = call("POST", "/flag", {"account_id": "ACC-1012", "reason": "x"})
        assert status == 409 and "closed" in body["error"]["message"]

    def test_connection_is_usable_after_a_rejected_flag(self, ledger):
        call("POST", "/flag", {"account_id": "ACC-0000", "reason": "x"})
        assert call("GET", "/health")[0] == 200

    def test_the_api_user_cannot_bypass_the_function(self, ledger):
        """The only way the API can flag an account is flag_account."""
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("UPDATE accounts SET status = 'FLAGGED' WHERE account_id = 'ACC-1004'")
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("INSERT INTO risk_events (account_id, event_type, reason) VALUES ('ACC-1004', 'MANUAL_FLAG', 'x')")

    def test_other_database_users_cannot_call_the_function(self, ledger, as_db_user):
        for user in ("fintrust_score", "fintrust_report"):
            as_db_user(user)
            with pytest.raises(DatabaseError, match="permission denied for function flag_account"):
                db.fetch_one("SELECT flag_account('ACC-1004', 'x', 't.nkosi')")


class TestAlertsAndResolve:
    def test_list_all_alerts_newest_first_with_transaction_details(self, ledger):
        status, body = call("GET", "/alerts", params={"limit": "100"})
        assert status == 200
        assert body["pagination"]["total"] == 12
        created = [item["created_at"] for item in body["items"]]
        assert created == sorted(created, reverse=True)
        first = body["items"][0]
        assert {"alert_id", "status", "transaction_id", "account_id", "amount", "risk_score", "risk_level"} <= set(first)
        assert all(item["risk_level"] == "HIGH" for item in body["items"])

    def test_filter_by_status(self, ledger):
        open_alerts = call("GET", "/alerts", params={"status": "OPEN"})[1]
        resolved = call("GET", "/alerts", params={"status": "RESOLVED", "limit": "100"})[1]
        assert open_alerts["pagination"]["total"] == 3 and resolved["pagination"]["total"] == 9
        assert {item["status"] for item in open_alerts["items"]} == {"OPEN"}
        assert all(item["resolved_by"] for item in resolved["items"])

    def test_no_customer_details_in_alerts(self, ledger):
        text = json.dumps(call("GET", "/alerts", params={"limit": "100"})[1])
        assert "example.com" not in text and "+27" not in text

    def test_resolve_an_open_alert(self, ledger):
        alert_id = call("GET", "/alerts", params={"status": "OPEN"})[1]["items"][0]["alert_id"]
        status, body = call("POST", "/resolve", {"alert_id": alert_id,
                                                 "resolution_note": "Supporting documents checked"})
        assert status == 200
        assert body["alert_id"] == alert_id and body["status"] == "RESOLVED" and body["resolved_by"] == "t.nkosi"
        assert rows(ledger, "SELECT status, resolved_by, resolution_note, resolved_at IS NOT NULL FROM alerts "
                            "WHERE alert_id = %s", (alert_id,)) == [
            ("RESOLVED", "t.nkosi", "Supporting documents checked", True)]
        assert call("GET", "/alerts", params={"status": "OPEN"})[1]["pagination"]["total"] == 2
        assert call("GET", "/insights")[1]["open_alerts"] == 2

    def test_resolving_twice_is_409_and_keeps_the_first_resolution(self, ledger):
        alert_id = call("GET", "/alerts", params={"status": "OPEN"})[1]["items"][0]["alert_id"]
        assert call("POST", "/resolve", {"alert_id": alert_id})[0] == 200
        status, body = call("POST", "/resolve", {"alert_id": alert_id}, claims=SECOND_OFFICER)
        assert status == 409 and body["error"]["message"] == f"Alert {alert_id} is already resolved"
        assert rows(ledger, "SELECT resolved_by FROM alerts WHERE alert_id = %s", (alert_id,)) == [("t.nkosi",)]

    def test_unknown_alert_is_404(self, ledger):
        assert call("POST", "/resolve", {"alert_id": 999999})[0] == 404

    def test_resolve_cannot_change_which_transaction_an_alert_is_for(self, ledger):
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("UPDATE alerts SET transaction_id = 'X'")


def test_schema_functions_and_users_can_be_dropped_and_rebuilt(admin):
    """drop_all.sql followed by the five files in order must work on a database
    that already has everything (this is the documented reset procedure)."""
    from tests.conftest import SQL_DIR

    with admin.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM transactions WHERE transaction_id NOT LIKE 'TXN-TEST-%'")
        before = cursor.fetchone()[0]
        for name in ("drop_all.sql", "schema.sql", "functions.sql", "views.sql", "db_users.sql", "seed.sql"):
            cursor.execute((SQL_DIR / name).read_text())
        cursor.execute("SELECT count(*) FROM transactions")
        assert cursor.fetchone()[0] == before == 275
        cursor.execute("SELECT has_function_privilege('fintrust_query', 'flag_account(varchar, varchar, varchar)', "
                       "'EXECUTE')")
        assert cursor.fetchone()[0] is True
