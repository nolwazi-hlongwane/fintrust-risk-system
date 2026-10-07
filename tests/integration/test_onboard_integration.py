"""Integration test: scripts/onboard_account.py against a real PostgreSQL."""

import sys
from pathlib import Path

import pytest

from tests.conftest import _admin_connect

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import onboard_account  # noqa: E402

ACCOUNT = "ACC-9990"


@pytest.fixture
def connection(postgres_database):
    conn = _admin_connect(postgres_database)
    yield conn
    conn.rollback()
    with conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM accounts WHERE account_id = %s", (ACCOUNT,))
        cursor.execute("DELETE FROM customers WHERE customer_id = 'CUST-9990'")
    conn.close()


def test_onboarding_creates_one_customer_and_one_active_account(connection):
    assert onboard_account.onboard(ACCOUNT, connection) is True
    with connection.cursor() as cursor:
        cursor.execute("SELECT customer_id, account_type, status FROM accounts WHERE account_id = %s", (ACCOUNT,))
        assert cursor.fetchone() == ("CUST-9990", "CHEQUE", "ACTIVE")
        cursor.execute("SELECT count(*) FROM customers WHERE customer_id = 'CUST-9990'")
        assert cursor.fetchone()[0] == 1


def test_onboarding_twice_changes_nothing(connection):
    assert onboard_account.onboard(ACCOUNT, connection) is True
    assert onboard_account.onboard(ACCOUNT, connection) is False
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM accounts WHERE account_id = %s", (ACCOUNT,))
        assert cursor.fetchone()[0] == 1


def test_onboarding_an_existing_seed_account_is_harmless(connection):
    assert onboard_account.onboard("ACC-1001", connection) is False
    with connection.cursor() as cursor:
        cursor.execute("SELECT customer_id FROM accounts WHERE account_id = 'ACC-1001'")
        assert cursor.fetchone()[0] == "CUST-0001"          # still its real owner
        cursor.execute("SELECT count(*) FROM customers WHERE customer_id = 'CUST-1001'")
        assert cursor.fetchone()[0] == 0                    # and no stray customer was created
