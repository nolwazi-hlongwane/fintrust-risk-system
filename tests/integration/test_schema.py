"""Integration tests: the SQL schema, seed data and database users on a real PostgreSQL."""

import pytest
from psycopg2 import errors

from fintrust import db
from fintrust.db import DatabaseError

pytestmark = pytest.mark.integration

INSERT_TRANSACTION = """
    INSERT INTO transactions (transaction_id, account_id, tx_date, amount, tx_type, channel,
                              country_code, risk_score, risk_level, source_file)
    VALUES (%(transaction_id)s, %(account_id)s, %(tx_date)s, %(amount)s, %(tx_type)s, %(channel)s,
            %(country_code)s, %(risk_score)s, %(risk_level)s, %(source_file)s)
"""


def valid_row(**overrides):
    row = {
        "transaction_id": "TXN-TEST-000001",
        "account_id": "ACC-1001",
        "tx_date": "2026-10-01T10:00:00+02:00",
        "amount": "250.00",
        "tx_type": "DEBIT",
        "channel": "POS",
        "country_code": "ZA",
        "risk_score": 0,
        "risk_level": "LOW",
        "source_file": "tests/integration",
    }
    row.update(overrides)
    return row


def query(admin, sql, params=None):
    with admin.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


class TestStructure:
    def test_all_five_tables_exist(self, admin):
        rows = query(admin, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        assert {row[0] for row in rows} == {"customers", "accounts", "transactions", "risk_events", "alerts"}

    def test_indexes_on_account_id_and_transaction_date(self, admin):
        rows = query(admin, "SELECT indexname FROM pg_indexes WHERE tablename = 'transactions'")
        names = {row[0] for row in rows}
        assert {"idx_transactions_account_id", "idx_transactions_tx_date"} <= names

    def test_every_table_has_a_primary_key(self, admin):
        rows = query(admin, """
            SELECT conrelid::regclass::text FROM pg_constraint
            WHERE contype = 'p' AND connamespace = 'public'::regnamespace
        """)
        assert len(rows) == 5

    def test_foreign_keys(self, admin):
        rows = query(admin, """
            SELECT conname FROM pg_constraint
            WHERE contype = 'f' AND connamespace = 'public'::regnamespace
        """)
        assert {row[0] for row in rows} == {
            "fk_accounts_customer", "fk_transactions_account", "fk_risk_events_account",
            "fk_risk_events_transaction", "fk_alerts_transaction",
        }

    def test_amount_is_exact_numeric_not_float(self, admin):
        rows = query(admin, """
            SELECT data_type, numeric_precision, numeric_scale FROM information_schema.columns
            WHERE table_name = 'transactions' AND column_name = 'amount'
        """)
        assert rows == [("numeric", 14, 2)]

    def test_schema_cannot_be_applied_twice_and_leaves_no_half_state(self, admin):
        from tests.conftest import SQL_DIR

        with pytest.raises(errors.DuplicateTable):
            query(admin, (SQL_DIR / "schema.sql").read_text())
        with admin.cursor() as cursor:
            cursor.execute("ROLLBACK")  # schema.sql opened a transaction that then failed
        assert query(admin, "SELECT count(*) FROM transactions")[0][0] >= 200


class TestConstraints:
    def insert(self, admin, **overrides):
        with admin.cursor() as cursor:
            cursor.execute(INSERT_TRANSACTION, valid_row(**overrides))

    def test_valid_transaction_is_accepted(self, admin):
        self.insert(admin)
        assert query(admin, "SELECT amount FROM transactions WHERE transaction_id = 'TXN-TEST-000001'")

    def test_unknown_account_is_rejected_by_foreign_key(self, admin):
        with pytest.raises(errors.ForeignKeyViolation):
            self.insert(admin, account_id="ACC-9999")

    def test_duplicate_transaction_id_is_rejected(self, admin):
        self.insert(admin)
        with pytest.raises(errors.UniqueViolation):
            self.insert(admin)

    @pytest.mark.parametrize("field", ["account_id", "tx_date", "amount", "tx_type", "channel",
                                       "country_code", "risk_score", "risk_level", "source_file"])
    def test_required_fields_are_not_null(self, admin, field):
        with pytest.raises(errors.NotNullViolation):
            self.insert(admin, **{field: None})

    @pytest.mark.parametrize("overrides", [
        {"amount": "0"},
        {"amount": "-50.00"},
        {"tx_type": "REFUND"},
        {"channel": "CARRIER_PIGEON"},
        {"country_code": "za"},
        {"risk_score": 101},
        {"risk_score": -1},
        {"risk_level": "CRITICAL"},
    ])
    def test_invalid_values_are_rejected_by_check_constraints(self, admin, overrides):
        with pytest.raises(errors.CheckViolation):
            self.insert(admin, **overrides)

    def test_a_rule_can_fire_only_once_per_transaction(self, admin):
        self.insert(admin)
        event = """INSERT INTO risk_events (account_id, transaction_id, event_type, points, reason)
                   VALUES ('ACC-1001', 'TXN-TEST-000001', 'CROSS_BORDER', 20, 'test')"""
        with admin.cursor() as cursor:
            cursor.execute(event)
            with pytest.raises(errors.UniqueViolation):
                cursor.execute(event)

    def test_rule_event_must_name_a_transaction(self, admin):
        with pytest.raises(errors.CheckViolation):
            admin.cursor().execute(
                "INSERT INTO risk_events (account_id, event_type, points, reason) "
                "VALUES ('ACC-1001', 'HIGH_AMOUNT', 50, 'test')"
            )

    def test_only_one_alert_per_transaction(self, admin):
        self.insert(admin)
        cursor = admin.cursor()
        cursor.execute("INSERT INTO alerts (transaction_id) VALUES ('TXN-TEST-000001')")
        with pytest.raises(errors.UniqueViolation):
            cursor.execute("INSERT INTO alerts (transaction_id) VALUES ('TXN-TEST-000001')")

    def test_resolved_alert_must_say_who_and_when(self, admin):
        self.insert(admin)
        with pytest.raises(errors.CheckViolation):
            admin.cursor().execute(
                "INSERT INTO alerts (transaction_id, status) VALUES ('TXN-TEST-000001', 'RESOLVED')"
            )

    def test_account_with_transactions_cannot_be_deleted(self, admin):
        with pytest.raises(errors.ForeignKeyViolation):
            admin.cursor().execute("DELETE FROM accounts WHERE account_id = 'ACC-1001'")


class TestSeedData:
    def test_at_least_200_transactions(self, admin):
        assert query(admin, "SELECT count(*) FROM transactions")[0][0] >= 200

    def test_covers_three_months(self, admin):
        rows = query(admin, "SELECT DISTINCT to_char(tx_date AT TIME ZONE 'Africa/Johannesburg', 'YYYY-MM') "
                            "FROM transactions ORDER BY 1")
        assert [row[0] for row in rows] == ["2026-07", "2026-08", "2026-09"]

    def test_multiple_accounts_types_and_risk_levels(self, admin):
        assert query(admin, "SELECT count(DISTINCT account_id) FROM transactions")[0][0] >= 10
        assert query(admin, "SELECT count(DISTINCT tx_type) FROM transactions")[0][0] == 2
        assert query(admin, "SELECT count(DISTINCT risk_level) FROM transactions")[0][0] == 3

    def test_score_equals_the_sum_of_its_rule_points(self, admin):
        rows = query(admin, """
            SELECT t.transaction_id
            FROM transactions t
            LEFT JOIN risk_events e ON e.transaction_id = t.transaction_id
            GROUP BY t.transaction_id, t.risk_score
            HAVING t.risk_score <> LEAST(100, COALESCE(SUM(e.points), 0))
        """)
        assert rows == []

    def test_level_matches_score_thresholds(self, admin):
        rows = query(admin, """
            SELECT transaction_id FROM transactions
            WHERE risk_level <> CASE WHEN risk_score >= 70 THEN 'HIGH'
                                     WHEN risk_score >= 40 THEN 'MEDIUM' ELSE 'LOW' END
        """)
        assert rows == []

    def test_every_high_risk_transaction_has_exactly_one_alert(self, admin):
        rows = query(admin, """
            SELECT t.transaction_id FROM transactions t
            LEFT JOIN alerts a ON a.transaction_id = t.transaction_id
            WHERE (t.risk_level = 'HIGH') <> (a.alert_id IS NOT NULL)
        """)
        assert rows == []

    def test_known_patterns_for_report_verification(self, admin):
        rows = dict(query(admin, """
            SELECT account_id, count(*) FROM transactions
            WHERE risk_level = 'HIGH' GROUP BY account_id
        """))
        assert rows == {"ACC-1003": 1, "ACC-1005": 4, "ACC-1007": 5, "ACC-1011": 2}

    def test_no_real_contact_details(self, admin):
        rows = query(admin, "SELECT email, phone_number FROM customers")
        assert all("example.com" in email for email, _phone in rows)
        assert all(" 555 01" in phone for _email, phone in rows)


class TestDatabaseUsers:
    def test_score_user_can_insert_into_the_ledger(self, as_db_user, admin):
        as_db_user("fintrust_score")
        assert db.execute(INSERT_TRANSACTION, valid_row()) == 1

    def test_score_user_cannot_change_or_delete_ledger_rows(self, as_db_user, admin):
        as_db_user("fintrust_score")
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("UPDATE transactions SET amount = 1 WHERE account_id = 'ACC-1001'")
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("DELETE FROM transactions WHERE account_id = 'ACC-1001'")

    def test_score_user_cannot_read_customer_details(self, as_db_user):
        as_db_user("fintrust_score")
        with pytest.raises(DatabaseError, match="permission denied"):
            db.fetch_all("SELECT full_name FROM customers")

    def test_report_user_is_read_only(self, as_db_user):
        as_db_user("fintrust_report")
        assert db.fetch_one("SELECT count(*) AS n FROM transactions")["n"] >= 200
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute(INSERT_TRANSACTION, valid_row())
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("UPDATE alerts SET status = 'OPEN'")

    def test_query_user_can_resolve_an_alert_but_nothing_else(self, as_db_user, admin):
        with admin.cursor() as cursor:
            cursor.execute(INSERT_TRANSACTION, valid_row(risk_score=70, risk_level="HIGH"))
            cursor.execute("INSERT INTO alerts (transaction_id) VALUES ('TXN-TEST-000001')")
        as_db_user("fintrust_query")
        changed = db.execute(
            "UPDATE alerts SET status = 'RESOLVED', resolved_at = now(), resolved_by = %s "
            "WHERE transaction_id = %s",
            ("test.officer", "TXN-TEST-000001"),
        )
        assert changed == 1
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("UPDATE alerts SET transaction_id = 'X' WHERE transaction_id = 'TXN-TEST-000001'")
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("UPDATE transactions SET risk_score = 0")
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("DELETE FROM alerts")

    def test_users_cannot_create_tables(self, as_db_user):
        as_db_user("fintrust_report")
        with pytest.raises(DatabaseError, match="permission denied"):
            db.execute("CREATE TABLE sneaky (id int)")

    def test_wrong_password_cannot_connect(self, as_db_user, monkeypatch):
        as_db_user("fintrust_report")
        monkeypatch.setenv("FINTRUST_DB_PASSWORD", "wrong")
        try:
            db.get_connection()
        except DatabaseError:
            return
        # A local server configured with "trust" authentication accepts any password.
        pytest.skip("server uses trust authentication")
