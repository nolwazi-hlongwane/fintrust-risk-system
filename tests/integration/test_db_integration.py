"""Integration tests: fintrust.db against a real PostgreSQL.

These prove on a real database what tests/unit/test_db.py proves with a fake:
commit, rollback, and connection reuse.
"""

import pytest

from fintrust import db
from fintrust.db import DatabaseError
from tests.integration.test_schema import INSERT_TRANSACTION, valid_row

pytestmark = pytest.mark.integration


def count_test_rows(admin):
    with admin.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM transactions WHERE transaction_id LIKE 'TXN-TEST-%'")
        return cursor.fetchone()[0]


def test_fetch_one_and_fetch_all_return_dicts(as_db_user):
    as_db_user("fintrust_query")
    row = db.fetch_one("SELECT account_id, status FROM accounts WHERE account_id = %s", ("ACC-1007",))
    assert row == {"account_id": "ACC-1007", "status": "FLAGGED"}
    rows = db.fetch_all("SELECT account_id FROM accounts ORDER BY account_id LIMIT 2")
    assert rows == [{"account_id": "ACC-1001"}, {"account_id": "ACC-1002"}]


def test_fetch_one_returns_none_for_no_match(as_db_user):
    as_db_user("fintrust_query")
    assert db.fetch_one("SELECT 1 FROM accounts WHERE account_id = 'ACC-0000'") is None


def test_parameters_are_escaped_not_executed(as_db_user):
    as_db_user("fintrust_query")
    hostile = "ACC-1001'; DROP TABLE alerts; --"
    assert db.fetch_all("SELECT 1 FROM accounts WHERE account_id = %s", (hostile,)) == []
    assert db.fetch_one("SELECT count(*) AS n FROM alerts")["n"] > 0


def test_block_commits_all_statements_together(as_db_user, admin):
    as_db_user("fintrust_score")
    with db.transaction() as cursor:
        cursor.execute(INSERT_TRANSACTION, valid_row(transaction_id="TXN-TEST-A"))
        cursor.execute(INSERT_TRANSACTION, valid_row(transaction_id="TXN-TEST-B"))
    assert count_test_rows(admin) == 2


def test_failure_rolls_back_everything_in_the_block(as_db_user, admin):
    """The first insert is valid; the second breaks the foreign key.
    Neither may remain: a transaction is all or nothing."""
    as_db_user("fintrust_score")
    with pytest.raises(DatabaseError, match="fk_transactions_account"):
        with db.transaction() as cursor:
            cursor.execute(INSERT_TRANSACTION, valid_row(transaction_id="TXN-TEST-A"))
            cursor.execute(INSERT_TRANSACTION, valid_row(transaction_id="TXN-TEST-B", account_id="ACC-9999"))
    assert count_test_rows(admin) == 0


def test_python_error_inside_block_rolls_back(as_db_user, admin):
    as_db_user("fintrust_score")
    with pytest.raises(RuntimeError):
        with db.transaction() as cursor:
            cursor.execute(INSERT_TRANSACTION, valid_row(transaction_id="TXN-TEST-A"))
            raise RuntimeError("bug in calling code")
    assert count_test_rows(admin) == 0


def test_connection_is_reused_and_still_usable_after_an_error(as_db_user, admin):
    as_db_user("fintrust_score")
    first = db.get_connection()
    with pytest.raises(DatabaseError):
        db.execute(INSERT_TRANSACTION, valid_row(amount="-1"))
    # Without the rollback, PostgreSQL would answer every later statement with
    # "current transaction is aborted".
    assert db.execute(INSERT_TRANSACTION, valid_row(transaction_id="TXN-TEST-A")) == 1
    assert db.get_connection() is first


def test_same_backend_process_serves_every_call(as_db_user):
    as_db_user("fintrust_query")
    pids = {db.fetch_one("SELECT pg_backend_pid() AS pid")["pid"] for _ in range(5)}
    assert len(pids) == 1


def test_recovers_after_the_server_drops_the_connection(as_db_user, admin):
    """Simulates RDS restarting or failing over."""
    as_db_user("fintrust_query")
    pid = db.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
    with admin.cursor() as cursor:
        cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
    with pytest.raises(DatabaseError):
        db.fetch_one("SELECT 1 AS ok")
    assert db.fetch_one("SELECT 1 AS ok") == {"ok": 1}
    assert db.fetch_one("SELECT pg_backend_pid() AS pid")["pid"] != pid


def test_slow_statement_is_cancelled_by_timeout(as_db_user):
    as_db_user("fintrust_query")
    with pytest.raises(DatabaseError, match="statement timeout"):
        with db.transaction() as cursor:
            cursor.execute("SET LOCAL statement_timeout = 200")
            cursor.execute("SELECT pg_sleep(2)")
    assert db.fetch_one("SELECT 1 AS ok") == {"ok": 1}


def test_is_healthy(as_db_user):
    as_db_user("fintrust_query")
    assert db.is_healthy() is True


def test_is_healthy_false_when_host_is_unreachable(as_db_user, monkeypatch):
    as_db_user("fintrust_query")
    db.close_connection()
    monkeypatch.setenv("FINTRUST_DB_PORT", "1")
    assert db.is_healthy() is False
