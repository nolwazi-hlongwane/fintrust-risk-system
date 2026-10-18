"""Unit tests for the chart routes (Week 15 Day 1): time buckets, validation,
and the shape of the answer, with the database stubbed. The SQL is tested
against a real PostgreSQL in tests/integration/test_trends_integration.py."""

import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from fintrust import db, query
from tests.conftest import signed_in


class Context:
    aws_request_id = "lambda-request-id"


def call(resource, params=None, path=None):
    response = query.handler(
        {"httpMethod": "GET", "resource": resource, "queryStringParameters": params, "pathParameters": path,
         "requestContext": signed_in("api-request-id")},
        Context(),
    )
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture(autouse=True)
def no_ssm(monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")


@pytest.fixture
def cursor():
    fake = MagicMock(name="cursor")
    fake.fetchall.return_value = []

    @contextmanager
    def fake_transaction():
        yield fake

    with patch.object(db, "transaction", fake_transaction):
        yield fake


class TestGranularity:
    @pytest.mark.parametrize(("days", "expected"), [(1, "day"), (62, "day"), (63, "week"), (730, "week"), (731, "month")])
    def test_bar_width_follows_the_length_of_the_period(self, days, expected):
        first = date(2026, 1, 1)
        assert query.choose_granularity(first, date.fromordinal(first.toordinal() + days - 1)) == expected

    def test_weeks_start_on_monday_like_postgresql(self):
        assert query.bucket_start(date(2026, 7, 1), "week") == date(2026, 6, 29)    # a Wednesday
        assert query.bucket_start(date(2026, 6, 29), "week") == date(2026, 6, 29)   # already a Monday
        assert query.bucket_start(date(2026, 7, 5), "week") == date(2026, 6, 29)    # a Sunday

    def test_month_and_day_buckets(self):
        assert query.bucket_start(date(2026, 9, 30), "month") == date(2026, 9, 1)
        assert query.bucket_start(date(2026, 9, 30), "day") == date(2026, 9, 30)

    def test_every_bar_is_listed_even_when_empty(self):
        assert query.bucket_starts(date(2026, 9, 1), date(2026, 9, 3), "day") == [
            date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
        assert query.bucket_starts(date(2026, 7, 1), date(2026, 7, 20), "week") == [
            date(2026, 6, 29), date(2026, 7, 6), date(2026, 7, 13), date(2026, 7, 20)]

    def test_months_roll_over_the_year_and_short_months(self):
        assert query.bucket_starts(date(2025, 11, 30), date(2026, 3, 2), "month") == [
            date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]


class TestTrendsValidation:
    def test_bad_date_is_400_before_any_query(self, cursor):
        assert call("/insights/trends", {"to": "yesterday"})[0] == 400
        cursor.execute.assert_not_called()

    def test_reversed_period_is_400(self, cursor):
        status, body = call("/insights/trends", {"from": "2026-09-30", "to": "2026-09-01"})
        assert status == 400 and "from must not be after to" in body["error"]["message"]

    def test_empty_database_answers_with_empty_series(self, cursor):
        cursor.fetchone.side_effect = [
            {"first_day": None, "last_day": None},                       # nothing to draw
            {"median_hours_to_resolve": None, "open_now": 0},
        ]
        status, body = call("/insights/trends")
        assert status == 200
        assert body["granularity"] == "day" and body["activity"] == []
        assert body["alert_flow"]["periods"] == [] and body["account_ranking"]["items"] == []
        assert [b["from"] for b in body["score_distribution"]["buckets"]] == list(range(0, 100, 10))
        assert body["score_distribution"]["buckets"][-1]["to"] == 100

    def test_chosen_dates_are_used_without_asking_the_database_for_a_span(self, cursor):
        cursor.fetchone.side_effect = [{"median_hours_to_resolve": Decimal("6.0"), "open_now": 1}]
        status, body = call("/insights/trends", {"from": "2026-09-01", "to": "2026-09-02"})
        assert status == 200
        assert [day["period_start"] for day in body["activity"]] == ["2026-09-01", "2026-09-02"]
        assert body["activity"][0] == {"period_start": "2026-09-01", "low": 0, "medium": 0, "high": 0,
                                       "transactions": 0, "total_value": 0}
        assert body["alert_flow"]["median_hours_to_resolve"] == "6.0"

    def test_the_bar_width_is_passed_as_a_value_not_written_into_the_sql(self, cursor):
        cursor.fetchone.side_effect = [{"median_hours_to_resolve": None, "open_now": 0}]
        call("/insights/trends", {"from": "2026-09-01", "to": "2026-09-02"})
        activity_call = cursor.execute.call_args_list[0]
        assert "date_trunc(%s" in activity_call.args[0] and activity_call.args[1][0] == "day"

    def test_ranking_rows_get_the_burst_flag(self, cursor):
        cursor.fetchone.side_effect = [
            {"first_day": date(2026, 9, 1), "last_day": date(2026, 9, 1)},
            {"median_hours_to_resolve": None, "open_now": 0},
        ]
        row = {"account_id": "ACC-1007", "account_status": "FLAGGED", "transaction_count": 9, "high_risk_count": 4,
               "total_risk_points": 300, "fraud_rank": 1, "fraud_percentile": Decimal("1.0000"),
               "accounts_ranked": 2, "peak_high_risk_in_30_days": 4}
        quiet = dict(row, account_id="ACC-1001", fraud_rank=2, peak_high_risk_in_30_days=3)
        cursor.fetchall.side_effect = [[], [{"bucket": 9, "risk_level": "HIGH", "transactions": 4}], [], [], [row, quiet]]
        body = call("/insights/trends")[1]
        assert body["account_ranking"]["accounts_ranked"] == 2
        assert [r["burst_detected"] for r in body["account_ranking"]["items"]] == [True, False]   # 4 > 3, 3 is not
        assert "accounts_ranked" not in body["account_ranking"]["items"][0]
        assert body["score_distribution"]["buckets"][9]["high"] == 4


def point(n, amount, tx_type="CREDIT", balance=None, level="LOW"):
    signed = Decimal(amount) if tx_type == "CREDIT" else -Decimal(amount)
    return {"transaction_id": f"TXN-{n:06d}", "tx_date": datetime(2026, 9, 1, tzinfo=timezone.utc), "tx_type": tx_type,
            "amount": Decimal(amount), "signed_amount": signed, "running_balance": Decimal(balance),
            "risk_level": level, "risk_score": 80 if level == "HIGH" else 0}


class TestBalance:
    @pytest.mark.parametrize("value", ["", "1007", "ACC-10", "ACC-1007; DROP TABLE accounts"])
    def test_bad_account_id_is_400(self, cursor, value):
        assert call("/accounts/{account_id}/balance", path={"account_id": value})[0] == 400
        cursor.execute.assert_not_called()

    def test_unknown_account_is_404(self, cursor):
        cursor.fetchone.return_value = None
        status, body = call("/accounts/{account_id}/balance", path={"account_id": "ACC-9999"})
        assert status == 404 and body["error"]["code"] == "NOT_FOUND"

    def test_account_id_is_case_insensitive(self, cursor):
        cursor.fetchone.return_value = {"status": "ACTIVE"}
        status, body = call("/accounts/{account_id}/balance", path={"account_id": "acc-1001"})
        assert status == 200 and body["account_id"] == "ACC-1001"

    def test_points_come_back_oldest_first_with_a_summary(self, cursor):
        cursor.fetchone.return_value = {"status": "FLAGGED"}
        # The database returns newest first (so the limit keeps the latest points).
        cursor.fetchall.return_value = [
            point(3, "50.00", "DEBIT", "1150.00", "HIGH"),
            point(2, "200.00", "CREDIT", "1200.00"),
            point(1, "100.00", "CREDIT", "1000.00"),
        ]
        status, body = call("/accounts/{account_id}/balance", {"from": "2026-09-01"}, {"account_id": "ACC-1007"})
        assert status == 200
        assert [p["transaction_id"] for p in body["points"]] == ["TXN-000001", "TXN-000002", "TXN-000003"]
        assert body["summary"] == {
            "opening_balance": "900.00", "closing_balance": "1150.00", "lowest_balance": "1000.00",
            "highest_balance": "1200.00", "credits": "300.00", "debits": "50.00", "high_risk_transactions": 1}
        assert body["truncated"] is False and body["account_status"] == "FLAGGED"

    def test_the_running_balance_comes_from_the_projects_sql_file(self, cursor):
        cursor.fetchone.return_value = {"status": "ACTIVE"}
        call("/accounts/{account_id}/balance", {"from": "2026-09-01", "to": "2026-09-30"}, {"account_id": "ACC-1001"})
        sql, values = cursor.execute.call_args_list[1].args
        assert "ROWS UNBOUNDED PRECEDING" in sql and "PARTITION BY account_id" in sql
        # The date filter is outside the window function, so the balance is not restarted at zero.
        assert sql.index("ROWS UNBOUNDED PRECEDING") < sql.index("s.tx_date >= %s")
        assert values[0] == "ACC-1001" and values[-1] == query.MAX_BALANCE_POINTS + 1

    def test_long_history_is_cut_to_the_latest_points_and_says_so(self, cursor, monkeypatch):
        monkeypatch.setattr(query, "MAX_BALANCE_POINTS", 2)
        cursor.fetchone.return_value = {"status": "ACTIVE"}
        cursor.fetchall.return_value = [point(3, "1.00", balance="3.00"), point(2, "1.00", balance="2.00"),
                                        point(1, "1.00", balance="1.00")]
        body = call("/accounts/{account_id}/balance", path={"account_id": "ACC-1001"})[1]
        assert body["truncated"] is True
        assert [p["transaction_id"] for p in body["points"]] == ["TXN-000002", "TXN-000003"]
        assert body["summary"]["opening_balance"] == "1.00"

    def test_missing_sql_file_is_a_500_without_detail(self, cursor, monkeypatch):
        monkeypatch.setattr(db, "SQL_DIR", db.SQL_DIR / "missing")
        status, body = call("/accounts/{account_id}/balance", path={"account_id": "ACC-1001"})
        assert status == 500 and "missing" not in body["error"]["message"]
