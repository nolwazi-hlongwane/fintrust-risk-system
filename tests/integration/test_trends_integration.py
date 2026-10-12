"""Integration tests: the chart routes (Week 15 Day 1) against the seed data in a
real PostgreSQL, as the fintrust_query database user.

The charts are only worth showing if their numbers agree with everything else
in the system. So each series is reconciled with a second source: GET /insights,
the project's own SQL files (risk_report.sql, monthly_summary.sql), or the
hand-verified balances in docs/sql-analytics.md.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from fintrust import query
from tests.conftest import SQL_DIR, signed_in

pytestmark = pytest.mark.integration


class Context:
    aws_request_id = "test-request-id"


@pytest.fixture(autouse=True)
def as_query_user(as_db_user):
    as_db_user("fintrust_query")


def get(resource, params=None, path=None):
    response = query.handler(
        {"httpMethod": "GET", "resource": resource, "queryStringParameters": params, "pathParameters": path,
         "requestContext": signed_in("api-request-id")},
        Context(),
    )
    return response["statusCode"], json.loads(response["body"])


def trends(params=None):
    status, body = get("/insights/trends", params)
    assert status == 200
    return body


def balance(account_id, params=None):
    return get("/accounts/{account_id}/balance", params, {"account_id": account_id})


def run_file(admin, name):
    with admin.cursor() as cursor:
        cursor.execute((SQL_DIR / name).read_text().replace("%", "%%"))
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


class TestActivity:
    def test_whole_ledger_is_shown_by_week_and_reconciles_with_insights(self):
        body, insights = trends(), get("/insights")[1]
        assert body["granularity"] == "week"
        assert body["span"] == {"first_day": "2026-07-01", "last_day": "2026-09-30"}
        activity = body["activity"]
        assert sum(week["transactions"] for week in activity) == insights["totals"]["transactions"] == 275
        for level in ("LOW", "MEDIUM", "HIGH"):
            assert sum(week[level.lower()] for week in activity) == insights["by_risk_level"][level]
        assert sum(Decimal(week["total_value"]) for week in activity) == Decimal(insights["totals"]["total_value"])

    def test_weeks_start_on_monday_and_none_is_skipped(self):
        starts = [date.fromisoformat(week["period_start"]) for week in trends()["activity"]]
        assert starts[0] == date(2026, 6, 29)              # the Monday of the week of 1 July
        assert all(day.weekday() == 0 for day in starts)
        assert all((later - earlier).days == 7 for earlier, later in zip(starts, starts[1:], strict=False))

    def test_a_short_period_is_shown_by_day_including_quiet_days(self):
        body = trends({"from": "2026-09-01", "to": "2026-09-07"})
        assert body["granularity"] == "day"
        assert [day["period_start"] for day in body["activity"]] == [f"2026-09-0{n}" for n in range(1, 8)]
        burst_day = next(day for day in body["activity"] if day["period_start"] == "2026-09-05")
        assert burst_day["transactions"] == 16           # ACC-1003's 13-transaction burst falls on this day

    def test_days_are_south_african_calendar_days(self):
        """A transaction at 23:30 UTC on 4 September is 01:30 on 5 September in South Africa."""
        day = trends({"from": "2026-09-05", "to": "2026-09-05"})["activity"]
        insights = get("/insights", {"from": "2026-09-05", "to": "2026-09-05"})[1]
        assert len(day) == 1 and day[0]["transactions"] == insights["totals"]["transactions"]

    def test_period_with_no_data(self):
        body = trends({"from": "2020-01-01", "to": "2020-01-03"})
        assert [day["transactions"] for day in body["activity"]] == [0, 0, 0]
        assert body["account_ranking"] == {"accounts_ranked": 0, "items": [],
                                           "burst_rule": "more than 3 high-risk transactions in any 30 days"}
        assert body["alert_flow"]["raised"] == 0 and body["alert_flow"]["median_hours_to_resolve"] is None


class TestScoreDistribution:
    def test_every_transaction_is_in_exactly_one_bar(self):
        buckets = trends()["score_distribution"]["buckets"]
        assert len(buckets) == 10
        assert sum(b["low"] + b["medium"] + b["high"] for b in buckets) == 275

    def test_levels_sit_on_the_right_side_of_the_thresholds(self):
        distribution = trends()["score_distribution"]
        assert distribution["thresholds"] == {"medium": 40, "high": 70}
        for bucket in distribution["buckets"]:
            if bucket["to"] < 40:
                assert bucket["medium"] == bucket["high"] == 0
            if bucket["from"] >= 70:
                assert bucket["low"] == bucket["medium"] == 0

    def test_matches_a_count_made_directly_in_the_database(self, admin):
        with admin.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM transactions WHERE risk_score BETWEEN 90 AND 100")
            top = cursor.fetchone()[0]
        last = trends()["score_distribution"]["buckets"][-1]
        assert (last["from"], last["to"]) == (90, 100)
        assert last["low"] + last["medium"] + last["high"] == top


class TestAlertFlow:
    def test_totals_match_the_alerts_table(self):
        flow = trends()["alert_flow"]
        assert (flow["raised"], flow["resolved"], flow["open_now"]) == (12, 9, 3)
        assert sum(p["raised"] for p in flow["periods"]) == 12
        assert sum(p["resolved"] for p in flow["periods"]) == 9

    def test_median_time_to_resolve(self, admin):
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT round((percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM resolved_at - created_at))"
                " / 3600)::numeric, 1) FROM alerts WHERE status = 'RESOLVED'")
            expected = cursor.fetchone()[0]
        assert Decimal(trends()["alert_flow"]["median_hours_to_resolve"]) == expected


class TestAccountRanking:
    def test_ranking_agrees_with_risk_report_sql(self, admin):
        """The chart and the SQL artefact must give every reported account the same percentile and rank."""
        ranking = {row["account_id"]: row for row in trends()["account_ranking"]["items"]}
        report = run_file(admin, "risk_report.sql")
        assert report, "the seed data has at least one account breaking the 30-day rule"
        for row in report:
            assert Decimal(ranking[row["account_id"]]["fraud_percentile"]) == row["fraud_percentile"]
            assert ranking[row["account_id"]]["fraud_rank"] == row["fraud_rank"]
            assert ranking[row["account_id"]]["peak_high_risk_in_30_days"] == row["peak_high_risk_in_30_days"]

    def test_burst_flag_marks_exactly_the_accounts_in_the_risk_report(self, admin):
        flagged = {row["account_id"] for row in trends()["account_ranking"]["items"] if row["burst_detected"]}
        assert flagged == {row["account_id"] for row in run_file(admin, "risk_report.sql")} == {"ACC-1007"}

    def test_most_risk_points_is_not_the_same_as_a_burst(self):
        """ACC-1005 has the most risk points but its high-risk transactions are spread out."""
        top = trends()["account_ranking"]["items"][0]
        assert top["account_id"] == "ACC-1005" and top["fraud_percentile"] == "1.0000"
        assert top["burst_detected"] is False

    def test_percentile_is_over_all_accounts(self):
        ranking = trends()["account_ranking"]
        assert ranking["accounts_ranked"] == 12
        assert [row["fraud_rank"] for row in ranking["items"]] == sorted(row["fraud_rank"] for row in ranking["items"])
        acc_1007 = next(row for row in ranking["items"] if row["account_id"] == "ACC-1007")
        assert acc_1007["fraud_percentile"] == "0.9091"      # 10 of the other 11 accounts are below it

    def test_no_customer_details(self):
        row = trends()["account_ranking"]["items"][0]
        assert not {"customer_id", "full_name", "email", "phone_number"} & set(row)


class TestRunningBalance:
    def test_hand_verified_rows_from_the_documentation(self):
        status, body = balance("ACC-1001")
        assert status == 200
        assert [p["running_balance"] for p in body["points"][:8]] == [
            "7903.00", "6055.52", "3542.92", "2375.13", "1234.83", "-14.17", "-484.40", "28015.60"]

    def test_every_point_agrees_with_monthly_summary_sql(self, admin):
        expected = {row["transaction_id"]: row["running_balance"] for row in run_file(admin, "monthly_summary.sql")
                    if row["account_id"] == "ACC-1007"}
        points = balance("ACC-1007")[1]["points"]
        assert {p["transaction_id"]: Decimal(p["running_balance"]) for p in points} == expected

    def test_period_keeps_the_balance_carried_in_from_before(self):
        whole = balance("ACC-1007")[1]
        september = balance("ACC-1007", {"from": "2026-09-01", "to": "2026-09-30"})[1]
        shown = [p["transaction_id"] for p in september["points"]]
        before = whole["points"][: len(whole["points"]) - len(shown)]
        assert before and [p["transaction_id"] for p in whole["points"][len(before):]] == shown
        # Not restarted at zero: September opens on the balance July and August left behind.
        assert Decimal(september["summary"]["opening_balance"]) == Decimal(before[-1]["running_balance"]) != 0
        assert september["summary"]["closing_balance"] == whole["summary"]["closing_balance"]

    def test_summary_adds_up(self):
        summary = balance("ACC-1007")[1]["summary"]
        assert (Decimal(summary["opening_balance"]) + Decimal(summary["credits"]) - Decimal(summary["debits"])
                == Decimal(summary["closing_balance"]))
        assert summary["high_risk_transactions"] == 5

    def test_points_are_oldest_first_and_carry_the_risk_level(self):
        points = balance("ACC-1007")[1]["points"]
        assert [p["tx_date"] for p in points] == sorted(p["tx_date"] for p in points)
        assert {p["risk_level"] for p in points} <= {"LOW", "MEDIUM", "HIGH"}

    def test_unknown_account_is_404(self):
        status, body = balance("ACC-9999")
        assert status == 404 and body["error"]["code"] == "NOT_FOUND"

    def test_account_with_no_transactions_in_the_period(self):
        status, body = balance("ACC-1007", {"from": "2020-01-01", "to": "2020-01-31"})
        assert status == 200 and body["points"] == [] and body["summary"] is None

    def test_injection_attempt_is_rejected(self):
        assert balance("ACC-1007' OR '1'='1")[0] == 400
