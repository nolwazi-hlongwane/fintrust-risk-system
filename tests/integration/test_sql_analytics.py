"""Integration tests: the SQL analytics files, run on the seed data in a real PostgreSQL.

Each query's answer is checked against the same answer worked out a second,
independent way in plain Python from the raw rows. If the SQL and the Python
agree, a mistake would have to be made identically in both.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

import pytest

from fintrust import db, report
from tests.conftest import SQL_DIR

SEED_ONLY = "transaction_id LIKE 'TXN-2026%%'"      # leave out rows other tests add and remove


def rows(admin, sql, params=None):
    with admin.cursor() as cursor:
        cursor.execute(sql, params)
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def run_file(admin, name):
    return rows(admin, (SQL_DIR / name).read_text().replace("%", "%%"))


@pytest.fixture
def ledger(admin):
    return rows(admin, f"SELECT * FROM transactions WHERE {SEED_ONLY} ORDER BY account_id, tx_date, transaction_id")


class TestRunningBalance:
    def test_hand_verified_example_from_the_documentation(self, admin):
        """The eight rows worked out by hand in docs/sql-analytics.md."""
        result = [row for row in run_file(admin, "monthly_summary.sql") if row["account_id"] == "ACC-1001"][:8]
        assert [(row["tx_type"], str(row["amount"]), str(row["running_balance"])) for row in result] == [
            ("CREDIT", "7903.00", "7903.00"),
            ("DEBIT", "1847.48", "6055.52"),
            ("DEBIT", "2512.60", "3542.92"),
            ("DEBIT", "1167.79", "2375.13"),
            ("DEBIT", "1140.30", "1234.83"),
            ("DEBIT", "1249.00", "-14.17"),
            ("DEBIT", "470.23", "-484.40"),
            ("CREDIT", "28500.00", "28015.60"),
        ]

    def test_every_row_matches_a_balance_added_up_in_python(self, admin, ledger):
        expected, balance = {}, defaultdict(Decimal)
        for row in ledger:
            balance[row["account_id"]] += row["amount"] if row["tx_type"] == "CREDIT" else -row["amount"]
            expected[row["transaction_id"]] = balance[row["account_id"]]
        result = {row["transaction_id"]: row["running_balance"] for row in run_file(admin, "monthly_summary.sql")
                  if row["transaction_id"] in expected}
        assert len(result) == 275
        assert result == expected

    def test_balance_restarts_for_each_account(self, admin):
        result = run_file(admin, "monthly_summary.sql")
        first_rows = {}
        for row in result:
            first_rows.setdefault(row["account_id"], row)
        assert all(row["running_balance"] == row["signed_amount"] for row in first_rows.values())

    def test_month_is_the_south_african_calendar_month(self, admin):
        result = run_file(admin, "monthly_summary.sql")
        assert {row["tx_month"] for row in result if row["transaction_id"].startswith("TXN-2026")} == {
            "2026-07", "2026-08", "2026-09"}

    def test_file_uses_the_window_function_the_brief_specifies(self):
        text = " ".join((SQL_DIR / "monthly_summary.sql").read_text().split())
        assert "OVER (PARTITION BY account_id ORDER BY tx_date, transaction_id ROWS UNBOUNDED PRECEDING)" in text


class TestRiskReport:
    def brute_force(self, ledger):
        """Accounts with more than 3 HIGH transactions inside any 30 days, found by checking every window."""
        high = defaultdict(list)
        for row in ledger:
            if row["risk_level"] == "HIGH":
                high[row["account_id"]].append(row["tx_date"])
        peaks = {}
        for account, dates in high.items():
            peak = max(sum(1 for other in dates if end - timedelta(days=30) <= other <= end) for end in dates)
            if peak > 3:
                peaks[account] = peak
        return peaks

    def test_finds_exactly_the_accounts_a_brute_force_check_finds(self, admin, ledger):
        result = run_file(admin, "risk_report.sql")
        assert {row["account_id"]: row["peak_high_risk_in_30_days"] for row in result} == self.brute_force(ledger)
        assert [row["account_id"] for row in result] == ["ACC-1007"]

    def test_account_with_four_high_risk_spread_over_three_months_is_not_reported(self, admin, ledger):
        """ACC-1005 has 4 HIGH transactions in total but never more than 3 inside 30 days."""
        counts = defaultdict(int)
        for row in ledger:
            counts[row["account_id"]] += row["risk_level"] == "HIGH"
        assert counts["ACC-1005"] == 4
        assert "ACC-1005" not in [row["account_id"] for row in run_file(admin, "risk_report.sql")]

    def test_exactly_three_in_the_window_is_not_a_breach_but_a_fourth_is(self, admin):
        def reported():
            return [row["account_id"] for row in run_file(admin, "risk_report.sql")]

        def add(number, day):
            with admin.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO transactions (transaction_id, account_id, tx_date, amount, tx_type, channel, "
                    "country_code, risk_score, risk_level, source_file) VALUES "
                    "(%s, 'ACC-1009', %s, 60000, 'DEBIT', 'EFT', 'GB', 70, 'HIGH', 'tests')",
                    (f"TXN-TEST-RISK-{number}", f"2026-08-{day:02d}T10:00:00+02:00"),
                )

        add(1, 1), add(2, 10), add(3, 20)
        assert "ACC-1009" not in reported()            # 3 is not "more than 3"
        add(4, 31)                                     # exactly 30 days after the first: still inside the window
        assert "ACC-1009" in reported()

    def test_fourth_transaction_31_days_later_is_outside_the_window(self, admin):
        with admin.cursor() as cursor:
            for number, stamp in enumerate(
                    ["2026-08-01T10:00:00+02:00", "2026-08-10T10:00:00+02:00", "2026-08-20T10:00:00+02:00",
                     "2026-09-01T10:00:00+02:00"], start=1):
                cursor.execute(
                    "INSERT INTO transactions (transaction_id, account_id, tx_date, amount, tx_type, channel, "
                    "country_code, risk_score, risk_level, source_file) VALUES "
                    "(%s, 'ACC-1009', %s, 60000, 'DEBIT', 'EFT', 'GB', 70, 'HIGH', 'tests')",
                    (f"TXN-TEST-RISK-{number}", stamp),
                )
        assert "ACC-1009" not in [row["account_id"] for row in run_file(admin, "risk_report.sql")]

    def test_percent_rank_matches_the_formula(self, admin, ledger):
        """PERCENT_RANK = (rank - 1) / (rows - 1), ranking accounts by total risk points."""
        points = defaultdict(int)
        for row in ledger:
            points[row["account_id"]] += row["risk_score"]
        lower = sum(1 for total in points.values() if total < points["ACC-1007"])
        expected = round(Decimal(lower) / Decimal(len(points) - 1), 4)
        result = run_file(admin, "risk_report.sql")[0]
        assert result["fraud_percentile"] == expected == Decimal("0.9091")
        assert result["fraud_rank"] == 2               # ACC-1005 has more risk points in total
        assert result["total_risk_points"] == points["ACC-1007"]

    def test_file_is_built_from_ctes_and_uses_percent_rank(self):
        text = (SQL_DIR / "risk_report.sql").read_text()
        body = text[text.index("\nWITH "):]
        for step in ("high_risk AS", "rolling AS", "breaches AS", "account_totals AS", "ranked AS"):
            assert step in body
        assert "PERCENT_RANK() OVER" in body
        assert "RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW" in body


class TestViews:
    def test_dashboard_view_has_one_row_per_account(self, admin):
        view = rows(admin, "SELECT * FROM v_risk_dashboard")
        assert len(view) == len(rows(admin, "SELECT account_id FROM accounts")) == 12

    def test_dashboard_counts_match_the_tables(self, admin, ledger):
        view = {row["account_id"]: row for row in rows(admin, "SELECT * FROM v_risk_dashboard")}
        for account in ("ACC-1005", "ACC-1007", "ACC-1012"):
            mine = [row for row in ledger if row["account_id"] == account]
            assert view[account]["transaction_count"] == len(mine)
            assert view[account]["high_risk_count"] == sum(row["risk_level"] == "HIGH" for row in mine)
            assert view[account]["total_debits"] == sum(row["amount"] for row in mine if row["tx_type"] == "DEBIT")
            assert view[account]["total_risk_points"] == sum(row["risk_score"] for row in mine)

    def test_joins_do_not_inflate_the_counts(self, admin):
        """ACC-1007 has transactions, several alerts and a manual flag: the classic row-multiplying case."""
        row = rows(admin, "SELECT * FROM v_risk_dashboard WHERE account_id = 'ACC-1007'")[0]
        alerts = rows(admin, "SELECT a.status FROM alerts a JOIN transactions t USING (transaction_id) "
                             "WHERE t.account_id = 'ACC-1007'")
        assert row["open_alerts"] + row["resolved_alerts"] == len(alerts)
        assert row["transaction_count"] == 31 and row["manual_flags"] == 1

    def test_account_with_no_alerts_shows_zero_not_null(self, admin):
        row = rows(admin, "SELECT * FROM v_risk_dashboard WHERE account_id = 'ACC-1002'")[0]
        assert (row["open_alerts"], row["resolved_alerts"], row["manual_flags"], row["high_risk_count"]) == (0, 0, 0, 0)

    def test_open_alerts_view_lists_only_open_alerts_with_their_rules(self, admin):
        view = rows(admin, "SELECT * FROM v_open_alerts")
        assert len(view) == rows(admin, "SELECT count(*) AS n FROM alerts WHERE status = 'OPEN'")[0]["n"]
        assert all(row["rules_fired"] and row["hours_open"] >= 0 for row in view)
        assert [row["alert_raised_at"] for row in view] == sorted(row["alert_raised_at"] for row in view)

    @pytest.mark.parametrize("view", ["v_risk_dashboard", "v_open_alerts"])
    def test_views_expose_no_customer_contact_details(self, admin, view):
        columns = {row["column_name"] for row in rows(
            admin, "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (view,))}
        assert columns and not columns & {"full_name", "email", "phone_number"}

    def test_views_file_can_be_run_again(self, admin):
        with admin.cursor() as cursor:
            cursor.execute((SQL_DIR / "views.sql").read_text())


class TestPermissions:
    @pytest.mark.parametrize("user", ["fintrust_query", "fintrust_report"])
    def test_api_and_report_users_can_read_the_views(self, as_db_user, user):
        as_db_user(user)
        assert len(db.fetch_all("SELECT account_id FROM v_risk_dashboard")) == 12
        assert db.fetch_all("SELECT alert_id FROM v_open_alerts") is not None

    def test_score_user_cannot_read_the_views(self, as_db_user):
        as_db_user("fintrust_score")
        with pytest.raises(db.DatabaseError) as error:
            db.fetch_all("SELECT * FROM v_risk_dashboard")
        assert error.value.pgcode == "42501"   # insufficient_privilege

    def test_report_user_cannot_read_customer_details(self, as_db_user):
        as_db_user("fintrust_report")
        with pytest.raises(db.DatabaseError) as error:
            db.fetch_all("SELECT full_name FROM customers")
        assert error.value.pgcode == "42501"   # insufficient_privilege

    def test_report_user_can_run_both_report_files_but_cannot_write(self, as_db_user):
        as_db_user("fintrust_report")
        assert db.fetch_all(report.load_sql("risk_report.sql"))
        assert db.fetch_all(report.load_sql("monthly_summary.sql"))
        with pytest.raises(db.DatabaseError):
            db.execute("UPDATE accounts SET status = 'ACTIVE' WHERE account_id = 'ACC-1007'")
