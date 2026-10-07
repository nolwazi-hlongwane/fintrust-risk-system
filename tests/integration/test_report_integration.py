"""Integration tests: the report Lambda end to end, on the seed data in a real
PostgreSQL (as the read-only fintrust_report user) with a moto S3 bucket."""

import csv
import io
import json
from decimal import Decimal

import boto3
import pytest

from fintrust import report
from tests.conftest import REGION

BUCKET = "fintrust-capstone-reports-test"


class Context:
    aws_request_id = "test-request-id"


@pytest.fixture
def s3(aws, as_db_user, monkeypatch):
    as_db_user("fintrust_report")
    monkeypatch.setenv("FINTRUST_REPORTS_BUCKET", BUCKET)
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    return client


def run(s3, month):
    result = report.handler({"month": month}, Context())
    body = json.loads(s3.get_object(Bucket=BUCKET, Key=result["json_key"])["Body"].read())
    table = list(csv.DictReader(io.StringIO(s3.get_object(Bucket=BUCKET, Key=result["csv_key"])["Body"].read().decode())))
    return result, body, table


def test_september_report_matches_the_ledger(s3, admin):
    result, body, table = run(s3, "2026-09")

    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), count(DISTINCT account_id), "
            "       sum(amount) FILTER (WHERE tx_type = 'CREDIT'), sum(amount) FILTER (WHERE tx_type = 'DEBIT'), "
            "       count(*) FILTER (WHERE risk_level = 'HIGH') "
            "FROM transactions WHERE tx_date >= '2026-09-01T00:00:00+02:00' AND tx_date < '2026-10-01T00:00:00+02:00'")
        count, accounts, credits, debits, high = cursor.fetchone()

    summary = body["summary"]
    assert (summary["transactions"], summary["active_accounts"], summary["high_risk"]) == (count, accounts, high) == (99, 11, high)
    assert Decimal(summary["total_credits"]) == credits and Decimal(summary["total_debits"]) == debits
    assert Decimal(summary["net_movement"]) == credits - debits
    assert summary["low_risk"] + summary["medium_risk"] + summary["high_risk"] == count
    assert summary["control_totals_agree"] is True
    assert result["transactions"] == 99 and result["accounts"] == 11 == len(table)


def test_every_account_balance_adds_up(s3):
    _, body, _ = run(s3, "2026-09")
    for account in body["accounts"]:
        opening, closing = Decimal(account["opening_balance"]), Decimal(account["closing_balance"])
        assert opening + Decimal(account["total_credits"]) - Decimal(account["total_debits"]) == closing


def test_closing_balance_of_one_month_is_the_opening_balance_of_the_next(s3):
    _, august, _ = run(s3, "2026-08")
    _, september, _ = run(s3, "2026-09")
    closing = {a["account_id"]: a["closing_balance"] for a in august["accounts"]}
    opening = {a["account_id"]: a["opening_balance"] for a in september["accounts"]}
    shared = set(closing) & set(opening)
    assert len(shared) >= 10
    assert all(closing[account] == opening[account] for account in shared)


def test_hand_verified_account_appears_with_the_documented_balance(s3):
    """ACC-1001 at the end of July: 27166.45, from the worked example in docs/sql-analytics.md."""
    _, body, table = run(s3, "2026-07")
    account = next(a for a in body["accounts"] if a["account_id"] == "ACC-1001")
    assert (account["opening_balance"], account["closing_balance"], account["transaction_count"]) == ("0.00", "27166.45", 10)
    assert next(row for row in table if row["account_id"] == "ACC-1001")["closing_balance"] == "27166.45"


def test_high_risk_account_is_reported_and_marked_in_the_csv(s3):
    _, body, table = run(s3, "2026-09")
    assert [a["account_id"] for a in body["high_risk_accounts"]] == ["ACC-1007"]
    assert body["high_risk_accounts"][0]["peak_high_risk_in_30_days"] == 5
    assert body["high_risk_accounts"][0]["fraud_percentile"] == "0.9091"
    flagged = {row["account_id"]: row["more_than_3_high_risk_in_30_days"] for row in table}
    assert flagged["ACC-1007"] == "yes" and flagged["ACC-1001"] == "no"


def test_alert_numbers_and_rules_are_included(s3, admin):
    _, body, _ = run(s3, "2026-09")
    with admin.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM alerts WHERE status = 'OPEN'")
        open_now = cursor.fetchone()[0]
    assert body["summary"]["alerts"]["open_now"] == open_now == len(body["open_alerts"])
    assert set(body["summary"]["rules_fired"]) <= {"HIGH_AMOUNT", "HIGH_VELOCITY", "CROSS_BORDER"}
    assert body["summary"]["rules_fired"]["HIGH_AMOUNT"] >= 1


def test_month_with_no_transactions_still_produces_a_valid_report(s3):
    result, body, table = run(s3, "2025-01")
    assert result["transactions"] == 0 and table == []
    assert body["summary"]["total_credits"] == "0" and body["summary"]["control_totals_agree"] is True
    assert [a["account_id"] for a in body["high_risk_accounts"]] == ["ACC-1007"]     # the ledger-wide section


def test_report_contains_no_customer_names_or_contact_details(s3, admin):
    result, _, _ = run(s3, "2026-09")
    text = s3.get_object(Bucket=BUCKET, Key=result["json_key"])["Body"].read().decode()
    with admin.cursor() as cursor:
        cursor.execute("SELECT full_name, email, phone_number FROM customers")
        for full_name, email, phone in cursor.fetchall():
            assert full_name not in text and email not in text and phone not in text


def test_running_twice_gives_the_same_report(s3):
    _, first, _ = run(s3, "2026-08")
    _, second, _ = run(s3, "2026-08")
    first.pop("generated_at"), second.pop("generated_at")
    for body in (first, second):
        for alert in body["open_alerts"]:
            alert.pop("hours_open")
    assert first == second
