"""Unit tests for the report Lambda, with the database stubbed.

The SQL itself is tested against a real PostgreSQL in
tests/integration/test_sql_analytics.py and test_report_integration.py.
"""

import csv
import io
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest

from fintrust import db, report
from tests.conftest import REGION

BUCKET = "fintrust-capstone-reports-test"
UTC = timezone.utc


class Context:
    aws_request_id = "test-request-id"


def sample_report(month="2026-09"):
    return {
        "report": "FinTrust monthly transaction and risk report",
        "month": month,
        "generated_at": "2026-10-01T04:00:03Z",
        "summary": {"transactions": 3, "total_credits": Decimal("28500.00"), "control_totals_agree": True},
        "accounts": [
            {"account_id": "ACC-1001", "transaction_count": 2, "opening_balance": Decimal("50891.63"),
             "total_credits": Decimal("28500.00"), "total_debits": Decimal("295.94"),
             "closing_balance": Decimal("79095.69"), "high_risk_count": 0, "more_than_3_high_risk_in_30_days": False},
            {"account_id": "ACC-1007", "transaction_count": 1, "opening_balance": Decimal("0.00"),
             "total_credits": Decimal("0"), "total_debits": Decimal("104500.00"),
             "closing_balance": Decimal("-104500.00"), "high_risk_count": 1, "more_than_3_high_risk_in_30_days": True},
        ],
        "high_risk_accounts": [{"account_id": "ACC-1007", "first_breach_at": datetime(2026, 9, 9, 13, 5, tzinfo=UTC)}],
        "open_alerts": [],
    }


@pytest.fixture
def reports_bucket(aws, monkeypatch):
    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
    monkeypatch.setenv("FINTRUST_REPORTS_BUCKET", BUCKET)
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    return s3


class TestWhichMonth:
    def test_manual_run_uses_the_month_asked_for(self):
        assert report.resolve_month({"month": "2026-09"}) == "2026-09"

    def test_scheduled_run_reports_on_the_month_that_just_ended(self):
        # 04:00 UTC on 1 October is 06:00 in South Africa
        event = {"detail-type": "Scheduled Event", "time": "2026-10-01T04:00:00Z"}
        assert report.resolve_month(event) == "2026-09"

    def test_scheduled_time_is_used_even_if_the_run_is_retried_later(self):
        event = {"time": "2026-10-01T04:00:00Z"}
        assert report.resolve_month(event, now=datetime(2026, 11, 3, tzinfo=UTC)) == "2026-09"

    def test_no_event_means_last_month(self):
        assert report.resolve_month(None, now=datetime(2026, 10, 7, 9, 0, tzinfo=UTC)) == "2026-09"

    def test_january_goes_back_to_december_of_the_previous_year(self):
        assert report.previous_month(datetime(2027, 1, 1, 4, 0, tzinfo=UTC)) == "2026-12"

    def test_month_is_decided_in_south_african_time_not_utc(self):
        # 22:30 UTC on 30 September is already 00:30 on 1 October in South Africa
        assert report.previous_month(datetime(2026, 9, 30, 22, 30, tzinfo=UTC)) == "2026-09"
        assert report.previous_month(datetime(2026, 9, 30, 21, 30, tzinfo=UTC)) == "2026-08"

    @pytest.mark.parametrize("bad", ["2026-13", "2026-9", "26-09", "September", "2026-09-01", "", 202609, "2026-00"])
    def test_malformed_month_is_refused(self, bad):
        with pytest.raises(report.ReportError, match="month must look like"):
            report.resolve_month({"month": bad})

    def test_unreadable_event_time_falls_back_to_now(self):
        assert report.resolve_month({"time": "not a time"}, now=datetime(2026, 10, 7, tzinfo=UTC)) == "2026-09"


class TestMonthBounds:
    def test_bounds_are_midnight_to_midnight_in_south_african_time(self):
        start, end = report.month_bounds("2026-09")
        assert start.isoformat() == "2026-09-01T00:00:00+02:00"
        assert end.isoformat() == "2026-10-01T00:00:00+02:00"

    def test_december_ends_in_january_of_the_next_year(self):
        start, end = report.month_bounds("2026-12")
        assert (start.year, start.month, end.year, end.month) == (2026, 12, 2027, 1)

    def test_february_in_a_leap_year(self):
        start, end = report.month_bounds("2028-02")
        assert (end - start).days == 29


class TestSqlFiles:
    @pytest.mark.parametrize("name", ["monthly_summary.sql", "risk_report.sql"])
    def test_file_is_loaded_without_its_final_semicolon(self, name):
        text = report.load_sql(name)
        assert not text.rstrip().endswith(";")
        assert text.endswith("\n")          # so a closing bracket after it is not swallowed by a comment

    def test_running_balance_file_can_be_wrapped_as_a_subquery(self):
        sql = report.ACCOUNT_SUMMARY.format(monthly_summary=report.load_sql("monthly_summary.sql"))
        assert "ROWS UNBOUNDED PRECEDING" in sql
        assert sql.count("%s") == 1                      # the month is the only value, passed as a parameter
        assert sql.rstrip().endswith("ORDER BY s.account_id")

    def test_missing_file_is_a_report_error(self):
        with pytest.raises(report.ReportError, match="missing from the package"):
            report.load_sql("no_such_file.sql")

    def test_sql_files_take_no_values_from_python_string_formatting(self):
        for name in ("monthly_summary.sql", "risk_report.sql"):
            text = report.load_sql(name)
            assert "%s" not in text and "{" not in text


class TestCsv:
    def test_one_row_per_account_with_exact_money(self):
        rows = list(csv.DictReader(io.StringIO(report.to_csv(sample_report()))))
        assert [row["account_id"] for row in rows] == ["ACC-1001", "ACC-1007"]
        assert rows[0] == {
            "month": "2026-09", "account_id": "ACC-1001", "transaction_count": "2",
            "opening_balance": "50891.63", "total_credits": "28500.00", "total_debits": "295.94",
            "closing_balance": "79095.69", "high_risk_count": "0", "more_than_3_high_risk_in_30_days": "no",
        }
        assert rows[1]["closing_balance"] == "-104500.00" and rows[1]["more_than_3_high_risk_in_30_days"] == "yes"

    def test_empty_month_still_has_a_header(self):
        empty = {**sample_report(), "accounts": []}
        assert report.to_csv(empty).strip() == ",".join(report.CSV_COLUMNS)

    def test_csv_has_no_customer_details(self):
        assert not {"full_name", "email", "phone_number"} & set(report.CSV_COLUMNS)


class TestStore:
    def test_both_files_are_written_under_the_month(self, reports_bucket):
        keys = report.store(sample_report())
        assert keys == ("monthly/2026-09/fintrust-report-2026-09.json", "monthly/2026-09/fintrust-report-2026-09.csv")
        listed = [item["Key"] for item in reports_bucket.list_objects_v2(Bucket=BUCKET)["Contents"]]
        assert sorted(listed) == sorted(keys)

    def test_json_keeps_money_exact_and_dates_readable(self, reports_bucket):
        json_key, _ = report.store(sample_report())
        stored = reports_bucket.get_object(Bucket=BUCKET, Key=json_key)
        assert stored["ContentType"] == "application/json"
        body = json.loads(stored["Body"].read())
        assert body["accounts"][0]["closing_balance"] == "79095.69"          # text, not a rounded float
        assert body["high_risk_accounts"][0]["first_breach_at"].startswith("2026-09-09 13:05:00")

    def test_running_again_replaces_the_files(self, reports_bucket):
        report.store(sample_report())
        changed = sample_report()
        changed["summary"]["transactions"] = 99
        json_key, _ = report.store(changed)
        body = json.loads(reports_bucket.get_object(Bucket=BUCKET, Key=json_key)["Body"].read())
        assert body["summary"]["transactions"] == 99
        assert len(reports_bucket.list_objects_v2(Bucket=BUCKET)["Contents"]) == 2

    def test_s3_failure_is_a_report_error(self, aws, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_REPORTS_BUCKET", "bucket-that-does-not-exist")
        with pytest.raises(report.ReportError, match="Could not write the report"):
            report.store(sample_report())


class TestHandler:
    def test_returns_a_summary_of_what_was_written(self, reports_bucket):
        with patch.object(report, "build_report", return_value=sample_report()) as build:
            result = report.handler({"month": "2026-09"}, Context())
        build.assert_called_once_with("2026-09")
        assert result == {
            "month": "2026-09",
            "json_key": "monthly/2026-09/fintrust-report-2026-09.json",
            "csv_key": "monthly/2026-09/fintrust-report-2026-09.csv",
            "transactions": 3, "accounts": 2, "high_risk_accounts": 1, "open_alerts": 0,
            "control_totals_agree": True,
        }

    def test_database_failure_fails_the_invocation_and_writes_nothing(self, reports_bucket, capsys):
        with patch.object(report, "build_report", side_effect=db.DatabaseUnavailable("Could not connect")):
            with pytest.raises(db.DatabaseError):
                report.handler({"month": "2026-09"}, Context())
        assert "Contents" not in reports_bucket.list_objects_v2(Bucket=BUCKET)
        assert '"report_failed"' in capsys.readouterr().out

    def test_bad_month_fails_before_touching_the_database(self, reports_bucket):
        with patch.object(report, "build_report") as build:
            with pytest.raises(report.ReportError):
                report.handler({"month": "last month"}, Context())
        build.assert_not_called()

    def test_disagreeing_control_totals_are_logged_as_an_error(self, reports_bucket, capsys):
        broken = sample_report()
        broken["summary"]["control_totals_agree"] = False
        with patch.object(report, "build_report", return_value=broken):
            result = report.handler({"month": "2026-09"}, Context())
        assert result["control_totals_agree"] is False
        assert '"control_totals_disagree"' in capsys.readouterr().out

    def test_empty_month_is_written_with_a_warning(self, reports_bucket, capsys):
        empty = {**sample_report(), "accounts": [], "summary": {"transactions": 0, "control_totals_agree": True}}
        with patch.object(report, "build_report", return_value=empty):
            assert report.handler({"month": "2026-09"}, Context())["transactions"] == 0
        assert '"report_has_no_transactions"' in capsys.readouterr().out

    def test_s3_client_is_created_at_module_level(self):
        assert report._s3.meta.service_model.service_name == "s3"
