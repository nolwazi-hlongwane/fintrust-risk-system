"""End-to-end test of the whole deployed system, on the real AWS account.

    python scripts/e2e_test.py              full run, about 9 minutes
    python scripts/e2e_test.py --skip-dlq   without the dead-letter scenario, about 2 minutes

Before running: start the database (./scripts/rds_start.sh). The full run also
needs the tunnel open in another window (./scripts/db_tunnel.sh), because
recovering from the dead-letter queue means onboarding an account in the ledger.

It sends one new file (the same 12 rows as scripts/make_demo_file.py) through
the public API and follows it through every part of the system:

    1. upload      POST /upload answers 202; the file is in the raw bucket, encrypted
    2. ETL         valid rows reach the processed bucket; invalid rows are quarantined with reasons
    3. scoring     every valid transaction is in the ledger (RDS) with the right score
    4. event store each has its journey and rule breakdown (DynamoDB)
    5. alert       the HIGH transaction raised an alert and the email was sent (SES)
    6. API         insights, accounts and alerts reflect the new data; the alert can be resolved
    7. report      the report Lambda writes this month's report to the reports bucket
    8. resilience  a transaction that cannot be scored is retried 3 times, lands in the
                   dead-letter queue without holding up other accounts, and is
                   recovered by fixing the cause and redriving the queue

To keep the output as evidence:
    python scripts/e2e_test.py | tee docs/evidence/w14d5-e2e-output.txt

Each run adds 10 transactions and one demonstration account to the ledger and
sends one alert email (plus alarm emails when the dead-letter alarm fires and clears).
"""

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from make_demo_file import build as build_file
from verify_write_api_alerts import PREFIX, REGION, account_id, api_settings, call, check, results, session

RAW_BUCKET = f"{PREFIX}-raw-{account_id}"
PROCESSED_BUCKET = f"{PREFIX}-processed-{account_id}"
QUARANTINE_BUCKET = f"{PREFIX}-quarantine-{account_id}"
REPORTS_BUCKET = f"{PREFIX}-reports-{account_id}"
QUEUE = f"{PREFIX}-transactions.fifo"
DLQ = f"{PREFIX}-transactions-dlq.fifo"
DLQ_ARN = f"arn:aws:sqs:{REGION}:{account_id}:{DLQ}"
DLQ_ALARM = f"{PREFIX}-transactions-dlq-depth"
SAST = timezone(timedelta(hours=2))

# Row numbers in the file (see scripts/make_demo_file.py) and what each must become.
EXPECTED_SCORES = {1: (0, "LOW"), 2: (0, "LOW"), 3: (0, "LOW"), 4: (50, "MEDIUM"), 5: (70, "HIGH"),
                   9: (0, "LOW"), 10: (0, "LOW"), 11: (20, "LOW"), 12: (0, "LOW")}
INVALID_ROWS = (6, 7)
DEAD_LETTER_ROW = 8

# SQS FIFO ignores a message whose deduplication id it has seen in the last 5
# minutes. A redriven message keeps its id, so the redrive waits until the
# original send is more than 5 minutes old. (Overridable for local simulation.)
DEDUPLICATION_WAIT = int(os.environ.get("E2E_DEDUPLICATION_WAIT", "320"))

s3 = session.client("s3")
sqs = session.client("sqs")
cloudwatch = session.client("cloudwatch")
lambda_client = boto3.session.Session(region_name=REGION).client(
    "lambda", config=Config(read_timeout=120, retries={"max_attempts": 0}))


def wait_for(description, probe, seconds, every=4):
    """Call probe() until it returns something truthy or time runs out. Returns the last value."""
    print(f"  ... waiting for {description} (up to {seconds} seconds)")
    deadline = time.time() + seconds
    value = probe()
    while not value and time.time() < deadline:
        time.sleep(every)
        value = probe()
    return value


def read_object(bucket, key):
    try:
        stored = s3.get_object(Bucket=bucket, Key=key)
        return stored["Body"].read().decode("utf-8"), stored
    except ClientError:
        return None, None


def journey(url, key, transaction_id):
    status, body = call(url, "GET", f"/transactions/{transaction_id}/journey", key)
    return body if status == 200 else None


def ledger_row(url, key, transaction_id):
    status, body = call(url, "GET", "/transactions", key, {"transaction_id": transaction_id})
    items = (body or {}).get("items", []) if status == 200 else []
    return items[0] if items else None


def queue_depth(name):
    url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"])


# ------------------------------------------------------------------ the stages

def stage_upload(url, key, name, text):
    print("\n1. Upload through the API")
    status, accepted = call(url, "POST", "/upload", key, {"filename": name}, text, "text/csv")
    check("POST /upload answers 202 Accepted", status == 202 and (accepted or {}).get("status") == "ACCEPTED",
          f"HTTP {status}")
    if status != 202:
        return None
    object_key = accepted["object_key"]
    body, stored = read_object(RAW_BUCKET, object_key)
    check("the file is in the raw bucket, unchanged and encrypted with the project key",
          body == text and stored.get("ServerSideEncryption") == "aws:kms", object_key)
    return object_key


def stage_etl(object_key, ids):
    print("\n2. ETL: validate, quarantine, hand over")
    stem = object_key[:-4]
    clean = wait_for("the ingest Lambda", lambda: read_object(PROCESSED_BUCKET, f"{stem}.clean.csv")[0], 90)
    clean_ids = [row["transaction_id"] for row in csv.DictReader(io.StringIO(clean or ""))]
    valid = sorted(ids[n] for n in ids if n not in INVALID_ROWS)
    check("the 10 valid rows were written to the processed bucket", sorted(clean_ids) == valid,
          f"{len(clean_ids)} rows")

    rejected, _ = read_object(QUARANTINE_BUCKET, f"{stem}.rejected.csv")
    rejected_rows = list(csv.DictReader(io.StringIO(rejected or "")))
    check("the 2 invalid rows are in the quarantine bucket, each with its reason",
          sorted(row["transaction_id"] for row in rejected_rows) == sorted(ids[n] for n in INVALID_ROWS)
          and all(row.get("errors") for row in rejected_rows),
          "; ".join(row.get("errors", "")[:50] for row in rejected_rows))
    check("rows in = rows passed on + rows quarantined (nothing lost)", len(clean_ids) + len(rejected_rows) == len(ids))


def stage_scoring(url, key, ids):
    print("\n3. Scoring and the ledger (RDS)")
    expected_ids = {n: ids[n] for n in EXPECTED_SCORES}

    def all_scored():
        found = {n: ledger_row(url, key, transaction_id) for n, transaction_id in expected_ids.items()}
        return found if all(found.values()) else None

    found = wait_for("all 9 scorable transactions to reach the ledger", all_scored, 150) or {}
    check("all 9 scorable transactions are in the ledger", len(found) == 9, f"{len(found)} of 9")
    wrong = [f"row {n}: {found[n]['risk_score']} {found[n]['risk_level']}" for n in found
             if (found[n]["risk_score"], found[n]["risk_level"]) != EXPECTED_SCORES[n]]
    check("every score and risk level is exactly as the rules say (0, 20, 50 and 70 points)", found and not wrong,
          "; ".join(wrong))
    high = found.get(5) or {}
    check("money is stored exactly",
          high.get("amount") == "88750.00" and Decimal(high.get("amount", "0")) == Decimal("88750"))
    check("the invalid rows never reached the ledger",
          all(ledger_row(url, key, ids[n]) is None for n in INVALID_ROWS))

    print("\n4. Event store (DynamoDB)")
    high_journey = journey(url, key, ids[5]) or {}
    rules = {rule["rule"]: rule["points"] for rule in (high_journey.get("score") or {}).get("rules", [])}
    check("the HIGH transaction's score is explained rule by rule",
          rules == {"HIGH_AMOUNT": 50, "CROSS_BORDER": 20} and sum(rules.values()) == 70,
          ", ".join(f"{name} +{points}" for name, points in rules.items()))
    stages = [event["stage"] for event in (journey(url, key, ids[1]) or {}).get("events", [])]
    check("a transaction's journey is recorded step by step",
          stages[:4] == ["RECEIVED", "VALIDATED", "QUEUED", "SCORED"], " > ".join(stages))
    return bool(found)


def stage_alert(url, key, ids):
    print("\n5. Alert (DynamoDB stream, alert Lambda, SES)")
    alerted = wait_for("the alert email",
                       lambda: (journey(url, key, ids[5]) or {}).get("status") == "ALERTED", 90)
    check("the alert email for the HIGH transaction was sent", alerted)
    status, body = call(url, "GET", "/alerts", key, {"status": "OPEN", "limit": "100"})
    mine = [item for item in (body or {}).get("items", []) if item["transaction_id"] == ids[5]]
    check("an alert was opened in the ledger for it, in the same step as the transaction", len(mine) == 1)
    statuses = [(journey(url, key, ids[n]) or {}).get("status") for n in EXPECTED_SCORES if n != 5]
    check("no alert was raised for LOW or MEDIUM transactions", statuses and "ALERTED" not in statuses)
    return mine[0]["alert_id"] if mine else None


def stage_api(url, key, ids, alert_id):
    print("\n6. API for the compliance team")
    filters = {"account_id": "ACC-1007", "risk_level": "HIGH", "limit": "100"}
    status, body = call(url, "GET", "/transactions", key, filters)
    check("GET /transactions finds the new transaction by account and risk level",
          status == 200 and ids[5] in [item["transaction_id"] for item in body["items"]])
    status, insights = call(url, "GET", "/insights", key)
    check("GET /insights includes it in the totals",
          status == 200 and insights["by_risk_level"]["HIGH"] >= 1 and insights["totals"]["transactions"] >= 9)
    status, accounts = call(url, "GET", "/accounts", key, {"limit": "100"})
    check("GET /accounts ranks accounts by risk", status == 200 and len(accounts["items"]) >= 12)
    if alert_id:
        status, resolved = call(url, "POST", "/resolve", key, body={
            "alert_id": alert_id, "resolution_note": "Resolved by the end-to-end test"})
        check("POST /resolve closes the alert", status == 200 and resolved["status"] == "RESOLVED", f"HTTP {status}")
    status, _body = call(url, "GET", "/transactions", None)
    check("a request without the API key is refused", status == 403, f"HTTP {status}")


def stage_report():
    print("\n7. Monthly report (report Lambda, reports bucket)")
    month = datetime.now(SAST).strftime("%Y-%m")
    response = lambda_client.invoke(FunctionName=f"{PREFIX}-report", Payload=json.dumps({"month": month}).encode())
    result = json.loads(response["Payload"].read() or b"null")
    ok = not response.get("FunctionError") and isinstance(result, dict)
    check(f"the report Lambda produced the {month} report", ok, "" if ok else str(result)[:120])
    if not ok:
        return
    text, _ = read_object(REPORTS_BUCKET, result["json_key"])
    report = json.loads(text) if text else {}
    check("the JSON and CSV reports are in the reports bucket",
          bool(report) and read_object(REPORTS_BUCKET, result["csv_key"])[0] is not None, result["json_key"])
    check("the report's control totals agree and it includes today's transactions",
          report.get("summary", {}).get("control_totals_agree") is True and report["summary"]["transactions"] >= 9,
          f"{report.get('summary', {}).get('transactions')} transactions this month")


def stage_resilience(url, key, ids, new_account, uploaded_at):
    from onboard_account import onboard  # needs the database tunnel; imported only for this stage

    transaction_id = ids[DEAD_LETTER_ROW]
    print("\n8. Resilience: fail, dead-letter, recover")
    print(f"  {transaction_id} is for account {new_account}, which is not in the ledger.")

    def dead_lettered():
        found = journey(url, key, transaction_id) or {}
        return found if found.get("status") == "DEAD_LETTERED" else None

    found = wait_for("3 failed attempts (the queue retries after 60 seconds each time)", dead_lettered, 330,
                     every=10) or {}
    failures = [event for event in found.get("events", []) if event["stage"] == "SCORE_FAILED"]
    check("scoring was attempted exactly 3 times, each recorded with its reason",
          len(failures) == 3 and all("not in the ledger" in event.get("detail", "") for event in failures),
          f"{len(failures)} attempts")
    in_dlq = wait_for("the message to arrive in the dead-letter queue", lambda: queue_depth(DLQ) >= 1, 90, every=10)
    check("the message is in the dead-letter queue: not lost, not retried forever", in_dlq)
    check("the failed transaction is not in the ledger (the database transaction rolled back)",
          ledger_row(url, key, transaction_id) is None)
    check("other accounts were not held up: their transactions were scored before this one gave up",
          ledger_row(url, key, ids[9]) is not None)

    alarm = cloudwatch.describe_alarms(AlarmNames=[DLQ_ALARM])["MetricAlarms"]
    state = alarm[0]["StateValue"] if alarm else "missing"
    print(f"  note: the {DLQ_ALARM} alarm is currently {state}. SQS metrics can lag by several minutes;")
    print("        the alarm email arrives when the metric catches up.")

    print(f"  Fixing the cause: onboarding account {new_account} in the ledger.")
    created = onboard(new_account)
    check("the missing account was onboarded", created)

    remaining = DEDUPLICATION_WAIT - (time.time() - uploaded_at)
    if remaining > 0:
        print(f"  ... waiting {int(remaining)} seconds: SQS FIFO ignores a repeated message id for 5 minutes")
        time.sleep(remaining)
    sqs.start_message_move_task(SourceArn=DLQ_ARN)       # the same call as ./scripts/redrive_dlq.sh
    print("  Redrive started: messages are moving from the dead-letter queue back to the scoring queue.")

    def recovered():
        return ledger_row(url, key, transaction_id)

    row = wait_for("the redriven transaction to be scored", recovered, 180, every=6) or {}
    check("after the redrive the transaction is in the ledger with its score",
          (row.get("risk_score"), row.get("risk_level")) == (0, "LOW"),
          f"{row.get('risk_score')} {row.get('risk_level')}")
    final = journey(url, key, transaction_id) or {}
    stages = [event["stage"] for event in final.get("events", [])]
    check("its journey shows the whole story: 3 failures, then scored",
          final.get("status") == "SCORED" and stages.count("SCORE_FAILED") == 3 and stages[-1] == "SCORED",
          " > ".join(stages))
    check("the dead-letter queue and the scoring queue are empty again",
          wait_for("the queues to drain", lambda: queue_depth(DLQ) == 0 and queue_depth(QUEUE) == 0, 60, every=10))


def tunnel_is_open():
    import psycopg2
    from run_sql import connection_settings

    try:
        psycopg2.connect(connect_timeout=8, **connection_settings()).close()
        return True
    except psycopg2.OperationalError:
        return False


def main():
    parser = argparse.ArgumentParser(description="End-to-end test of the deployed FinTrust system.")
    parser.add_argument("--skip-dlq", action="store_true", help="skip the dead-letter scenario (no tunnel needed)")
    arguments = parser.parse_args()

    started = time.time()
    print(f"FinTrust end-to-end test, account {account_id}, region {REGION}")
    print(f"Started {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    try:
        url, key = api_settings()
        status, health = call(url, "GET", "/health", key)
        if status != 200:
            print(f"\nStopping: the API cannot reach the database (HTTP {status} {health}).", file=sys.stderr)
            print("Start it with ./scripts/rds_start.sh and try again.", file=sys.stderr)
            return 1
        if not arguments.skip_dlq and not tunnel_is_open():
            print("\nStopping: the database tunnel is not open; the dead-letter recovery needs it.", file=sys.stderr)
            print("Open it in another window (./scripts/db_tunnel.sh), or run with --skip-dlq.", file=sys.stderr)
            return 1

        name, text, _high_id, _dead_letter_id, new_account = build_file()
        lines = text.strip().split("\n")[1:]
        ids = {number: line.split(",")[0] for number, line in enumerate(lines, start=1)}
        print(f"File: {name} ({len(ids)} rows)")

        uploaded_at = time.time()
        object_key = stage_upload(url, key, name, text)
        if object_key:
            stage_etl(object_key, ids)
            if stage_scoring(url, key, ids):
                alert_id = stage_alert(url, key, ids)
                stage_api(url, key, ids, alert_id)
                stage_report()
            if arguments.skip_dlq:
                print("\n8. Resilience: skipped (--skip-dlq). The message for the unknown account will reach the")
                print(f"   dead-letter queue by itself. To clear it: python scripts/onboard_account.py {new_account}")
                print("   then ./scripts/redrive_dlq.sh")
            else:
                stage_resilience(url, key, ids, new_account, uploaded_at)
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    minutes, seconds = divmod(int(time.time() - started), 60)
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed in {minutes} min {seconds} s")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        return 1
    print("END-TO-END TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
