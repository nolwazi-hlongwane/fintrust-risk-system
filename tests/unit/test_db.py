"""Unit tests for fintrust.db using a fake psycopg2 connection.

These prove the two required patterns without needing a database:
connection reuse, and rollback on every error path. The same behaviour is
proved against a real PostgreSQL in tests/integration/test_db_integration.py.
"""

from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from fintrust import config, db
from fintrust.db import DatabaseError


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
    monkeypatch.setenv("FINTRUST_DB_HOST", "db.internal")
    monkeypatch.setenv("FINTRUST_DB_USER", "fintrust_score")
    monkeypatch.setenv("FINTRUST_DB_AUTH_MODE", "password")
    monkeypatch.setenv("FINTRUST_DB_PASSWORD", "local-test-only")
    db.close_connection()
    yield
    db.close_connection()


def fake_connection():
    connection = MagicMock(name="connection")
    connection.closed = 0
    connection.cursor.return_value.closed = False
    return connection


@pytest.fixture
def connect(settings):
    with patch("fintrust.db.psycopg2.connect") as mock_connect:
        mock_connect.side_effect = lambda **kwargs: fake_connection()
        yield mock_connect


class TestConnectionReuse:
    def test_connection_is_opened_once_and_reused(self, connect):
        first = db.get_connection()
        second = db.get_connection()
        db.fetch_all("SELECT 1")
        assert first is second
        assert connect.call_count == 1

    def test_reconnects_if_connection_was_closed(self, connect):
        first = db.get_connection()
        first.closed = 1
        second = db.get_connection()
        assert second is not first
        assert connect.call_count == 2

    def test_connect_failure_raises_database_error(self, settings):
        with patch("fintrust.db.psycopg2.connect", side_effect=psycopg2.OperationalError("refused")):
            with pytest.raises(DatabaseError, match="Could not connect"):
                db.get_connection()

    def test_connect_uses_timeouts(self, connect):
        db.get_connection()
        arguments = connect.call_args.kwargs
        assert arguments["connect_timeout"] == 5
        assert "statement_timeout" in arguments["options"]


class TestAuthentication:
    def test_password_mode_uses_configured_password(self, connect):
        db.get_connection()
        assert connect.call_args.kwargs["password"] == "local-test-only"

    def test_iam_mode_uses_signed_token_and_requires_tls(self, connect, monkeypatch):
        monkeypatch.setenv("FINTRUST_DB_AUTH_MODE", "iam")
        monkeypatch.delenv("FINTRUST_DB_PASSWORD")
        db.get_connection()
        arguments = connect.call_args.kwargs
        assert arguments["sslmode"] == "require"
        assert arguments["user"] == "fintrust_score"
        # The token is a signed URL for this host and user, not a stored secret.
        assert arguments["password"].startswith("db.internal:5432/?")
        assert "Action=connect" in arguments["password"]
        assert "DBUser=fintrust_score" in arguments["password"]
        assert "X-Amz-Signature=" in arguments["password"]

    def test_unknown_auth_mode_is_rejected(self, connect, monkeypatch):
        monkeypatch.setenv("FINTRUST_DB_AUTH_MODE", "kerberos")
        with pytest.raises(config.ConfigError, match="db_auth_mode"):
            db.get_connection()

    def test_rds_client_is_created_at_module_level(self):
        assert db._rds.meta.service_model.service_name == "rds"


class TestCommitAndRollback:
    def test_success_commits_once_and_does_not_roll_back(self, connect):
        with db.transaction() as cursor:
            cursor.execute("INSERT ...")
        connection = db.get_connection()
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()

    def test_database_error_rolls_back_and_raises(self, connect):
        connection = db.get_connection()
        connection.cursor.return_value.execute.side_effect = psycopg2.IntegrityError("fk violation")
        with pytest.raises(DatabaseError, match="fk violation"):
            with db.transaction() as cursor:
                cursor.execute("INSERT ...")
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_error_on_commit_rolls_back(self, connect):
        connection = db.get_connection()
        connection.commit.side_effect = psycopg2.IntegrityError("deferred constraint")
        with pytest.raises(DatabaseError):
            with db.transaction() as cursor:
                cursor.execute("INSERT ...")
        connection.rollback.assert_called_once()

    def test_non_database_error_also_rolls_back(self, connect):
        connection = db.get_connection()
        with pytest.raises(ZeroDivisionError):
            with db.transaction() as cursor:
                cursor.execute("INSERT ...")
                raise ZeroDivisionError
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_connection_is_kept_after_an_ordinary_error(self, connect):
        connection = db.get_connection()
        connection.cursor.return_value.execute.side_effect = psycopg2.IntegrityError("duplicate")
        with pytest.raises(DatabaseError):
            db.execute("INSERT ...")
        assert db.get_connection() is connection
        assert connect.call_count == 1

    def test_lost_connection_is_discarded_so_next_call_reconnects(self, connect):
        connection = db.get_connection()
        connection.cursor.return_value.execute.side_effect = psycopg2.OperationalError("server closed")
        with pytest.raises(DatabaseError):
            db.fetch_all("SELECT 1")
        connection.rollback.assert_called_once()
        assert db.get_connection() is not connection
        assert connect.call_count == 2

    def test_rollback_failure_does_not_hide_the_original_error(self, connect):
        connection = db.get_connection()
        connection.cursor.return_value.execute.side_effect = psycopg2.OperationalError("server closed")
        connection.rollback.side_effect = psycopg2.InterfaceError("connection already closed")
        with pytest.raises(DatabaseError, match="server closed"):
            db.fetch_all("SELECT 1")

    def test_cursor_is_always_closed(self, connect):
        connection = db.get_connection()
        cursor = connection.cursor.return_value
        cursor.execute.side_effect = psycopg2.DataError("bad value")
        with pytest.raises(DatabaseError):
            db.execute("UPDATE ...")
        cursor.close.assert_called_once()

    def test_failure_is_logged_with_sqlstate(self, connect, capsys):
        connection = db.get_connection()
        connection.cursor.return_value.execute.side_effect = psycopg2.IntegrityError("fk violation")
        with pytest.raises(DatabaseError):
            db.execute("INSERT ...")
        output = capsys.readouterr().out
        assert "db_transaction_rolled_back" in output
        assert "IntegrityError" in output


class TestHelpers:
    def test_fetch_all_returns_list_of_dicts(self, connect):
        db.get_connection().cursor.return_value.fetchall.return_value = [{"a": 1}, {"a": 2}]
        assert db.fetch_all("SELECT a") == [{"a": 1}, {"a": 2}]

    def test_fetch_one_returns_none_when_no_row(self, connect):
        db.get_connection().cursor.return_value.fetchone.return_value = None
        assert db.fetch_one("SELECT a") is None

    def test_execute_returns_row_count(self, connect):
        db.get_connection().cursor.return_value.rowcount = 3
        assert db.execute("UPDATE ...") == 3

    def test_parameters_are_passed_separately_from_sql(self, connect):
        db.fetch_all("SELECT * FROM transactions WHERE account_id = %s", ("ACC-1001",))
        db.get_connection().cursor.return_value.execute.assert_called_with(
            "SELECT * FROM transactions WHERE account_id = %s", ("ACC-1001",)
        )

    def test_is_healthy_true(self, connect):
        db.get_connection().cursor.return_value.fetchone.return_value = {"ok": 1}
        assert db.is_healthy() is True

    def test_is_healthy_false_when_database_is_down(self, settings):
        with patch("fintrust.db.psycopg2.connect", side_effect=psycopg2.OperationalError("down")):
            assert db.is_healthy() is False
