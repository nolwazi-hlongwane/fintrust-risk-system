"""Verify Week 14 Day 4 (part 1) on the deployed account: SQL analytics and the monthly report.

    python scripts/verify_report.py

The database must be running (./scripts/rds_start.sh).

It checks the report Lambda and its monthly schedule, then really runs the
Lambda for September 2026 (a month of seed data) and for the current month,
downloads the files it wrote and checks the numbers inside them:

    * the running balance: opening + credits - debits = closing, for every account
    * one month's closing balance is the next month's opening balance
    * the hand-verified balance from docs/sql-analytics.md
    * the high-risk account report (CTE, rolling 30 days, PERCENT_RANK)
    * the CSV and the JSON agree, and neither holds customer details

Running it changes nothing in the ledger. It rewrites the report files for the
months it runs.
"""

import csv
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from verify_write_api_alerts import PREFIX, REGION, account_id, check, results, session

FUNCTION = f"{PREFIX}-report"
RULE = f"{PREFIX}-monthly-report"
BUCKET = f"{PREFIX}-reports-{account_id}"
SAST = timezone(timedelta(hours=2))

# The report can take longer than boto3's default 60 second wait on a cold start.
lambda_client = boto3.session.Session(region_name=REGION).client(
    "lambda", config=Config(read_timeout=120, retries={"max_attempts": 0}))
events = session.client("events")
s3 = session.client("s3")
iam = session.client("iam")


def verify_configuration():
    print("\nReport Lambda and schedule")
    function = lambda_client.get_function_configuration(FunctionName=FUNCTION)
    check("report Lambda is active, in the VPC, with its own role",
          function["State"] == "Active" and function.get("VpcConfig", {}).get("VpcId")
          and function["Role"].endswith(f"/{PREFIX}-report-role"))
    variables = function["Environment"]["Variables"]
    check("it logs in as the read-only database user with an IAM token, and holds no password",
          variables.get("FINTRUST_DB_USER") == "fintrust_report" and variables.get("FINTRUST_DB_AUTH_MODE") == "iam"
          and not any("PASSWORD" in name for name in variables))

    policy = iam.get_role_policy(RoleName=f"{PREFIX}-report-role", PolicyName=f"{PREFIX}-report-data")["PolicyDocument"]
    s3_statements = [s for s in policy["Statement"] if any(a.startswith("s3:") for a in s["Action"])]
    check("its role may only write under monthly/ in the reports bucket",
          len(s3_statements) == 1 and s3_statements[0]["Action"] == ["s3:PutObject"]
          and s3_statements[0]["Resource"] == f"arn:aws:s3:::{BUCKET}/monthly/*")

    try:
        rule = events.describe_rule(Name=RULE)
    except ClientError:
        check("the monthly schedule exists", False, "run ./infrastructure/eventbridge/deploy_schedule.sh")
        return
    check("the schedule is 04:00 UTC (06:00 SAST) on the 1st of every month, and enabled",
          rule["ScheduleExpression"] == "cron(0 4 1 * ? *)" and rule["State"] == "ENABLED", rule["ScheduleExpression"])
    targets = events.list_targets_by_rule(Rule=RULE)["Targets"]
    check("the schedule's only target is the report Lambda",
          len(targets) == 1 and targets[0]["Arn"] == function["FunctionArn"])
    resource_policy = json.loads(lambda_client.get_policy(FunctionName=FUNCTION)["Policy"])
    allowed = [s for s in resource_policy["Statement"] if s["Principal"].get("Service") == "events.amazonaws.com"]
    check("only that rule may invoke the function",
          len(allowed) >= 1
          and all(s.get("Condition", {}).get("ArnLike", {}).get("AWS:SourceArn") == rule["Arn"] for s in allowed))


def run_report(payload):
    """Invoke the Lambda and wait for its answer. Returns (result dict or None, error text)."""
    response = lambda_client.invoke(FunctionName=FUNCTION, Payload=json.dumps(payload).encode())
    body = json.loads(response["Payload"].read() or b"null")
    if response.get("FunctionError"):
        return None, (body or {}).get("errorMessage", "the function failed")
    return body, ""


def download(key):
    stored = s3.get_object(Bucket=BUCKET, Key=key)
    return stored["Body"].read().decode("utf-8"), stored


def verify_september():
    print("\nSeptember 2026 report (seed data)")
    result, error = run_report({"month": "2026-09"})
    check("the Lambda produced the report", result is not None, error)
    if result is None:
        return None
    check("files are written under monthly/2026-09/",
          result["json_key"] == "monthly/2026-09/fintrust-report-2026-09.json"
          and result["csv_key"] == "monthly/2026-09/fintrust-report-2026-09.csv")

    text, stored = download(result["json_key"])
    report = json.loads(text)
    check("the files are encrypted with the project KMS key",
          stored.get("ServerSideEncryption") == "aws:kms" and "key/" in stored.get("SSEKMSKeyId", ""))
    summary = report["summary"]
    check("September has the 99 seed transactions across 11 accounts",
          summary["transactions"] == 99 and summary["active_accounts"] == 11 == len(report["accounts"]),
          f"{summary['transactions']} transactions, {summary['active_accounts']} accounts")
    check("LOW + MEDIUM + HIGH = all transactions",
          summary["low_risk"] + summary["medium_risk"] + summary["high_risk"] == summary["transactions"])
    check("the per-account rows add up to the month's totals (control totals)", summary["control_totals_agree"] is True)

    print("\nRunning balance (window function)")
    adds_up = all(
        Decimal(a["opening_balance"]) + Decimal(a["total_credits"]) - Decimal(a["total_debits"])
        == Decimal(a["closing_balance"]) for a in report["accounts"])
    check("opening + credits - debits = closing for every account", adds_up)

    print("\nHigh-risk accounts (CTE, rolling 30 days, PERCENT_RANK)")
    risky = {a["account_id"]: a for a in report["high_risk_accounts"]}
    check("ACC-1007 is reported: at least 5 high-risk transactions inside 30 days",
          "ACC-1007" in risky and risky["ACC-1007"]["peak_high_risk_in_30_days"] >= 5,
          f"peak {risky.get('ACC-1007', {}).get('peak_high_risk_in_30_days')}")
    check("ACC-1005 is not reported: 4 high-risk transactions, but never more than 3 inside 30 days",
          "ACC-1005" not in risky)
    percentiles = [Decimal(a["fraud_percentile"]) for a in report["high_risk_accounts"]]
    check("every fraud percentile is between 0 and 1", all(0 <= value <= 1 for value in percentiles),
          ", ".join(f"{a['account_id']} {a['fraud_percentile']}" for a in report["high_risk_accounts"]))

    print("\nCSV file")
    table = list(csv.DictReader(io.StringIO(download(result["csv_key"])[0])))
    check("the CSV has one row per account and the same closing balances as the JSON",
          {row["account_id"]: row["closing_balance"] for row in table}
          == {a["account_id"]: a["closing_balance"] for a in report["accounts"]})
    check("the CSV marks the high-risk account",
          {row["account_id"] for row in table if row["more_than_3_high_risk_in_30_days"] == "yes"} >= {"ACC-1007"})
    check("neither file contains customer names or contact details",
          not any(word in text for word in ("full_name", "email", "phone_number", "@")))
    return report


def verify_month_chain(september):
    print("\nMonth to month")
    july, error = run_report({"month": "2026-07"})
    august, error = (run_report({"month": "2026-08"}) if july else (None, error))
    check("July and August reports were produced", july is not None and august is not None, error)
    if not august:
        return
    july_report = json.loads(download(july["json_key"])[0])
    august_report = json.loads(download(august["json_key"])[0])
    hand = next((a for a in july_report["accounts"] if a["account_id"] == "ACC-1001"), {})
    check("ACC-1001 closes July on 27166.45, the hand-verified figure in docs/sql-analytics.md",
          hand.get("opening_balance") == "0.00" and hand.get("closing_balance") == "27166.45",
          f"closing {hand.get('closing_balance')}")
    closing = {a["account_id"]: a["closing_balance"] for a in august_report["accounts"]}
    opening = {a["account_id"]: a["opening_balance"] for a in september["accounts"]}
    shared = set(closing) & set(opening)
    check("August's closing balance is September's opening balance, for every account",
          len(shared) >= 10 and all(closing[a] == opening[a] for a in shared), f"{len(shared)} accounts compared")


def verify_scheduled_run():
    print("\nA scheduled run")
    now = datetime.now(timezone.utc)
    expected = (now.astimezone(SAST).replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    event = {"detail-type": "Scheduled Event", "source": "aws.events", "time": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    result, error = run_report(event)
    check(f"an event shaped like EventBridge's reports on last month ({expected})",
          result is not None and result["month"] == expected, error or (result or {}).get("month", ""))
    result, error = run_report({"month": "September"})
    check("a malformed month is refused, not guessed", result is None and "month must look like" in error, error)


def main():
    print(f"Verifying the monthly report in account {account_id}, region {REGION}")
    try:
        verify_configuration()
        september = verify_september()
        if september:
            verify_month_chain(september)
            verify_scheduled_run()
            print(f"\nReports are in:  s3://{BUCKET}/monthly/")
        else:
            print("\nStopping: is the database running?  ./scripts/rds_start.sh")
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        print("Logs:  aws logs tail /aws/lambda/fintrust-capstone-report --since 10m", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
