"""Verify the Week 13 Day 4 ETL Lambda on the deployed account.

    python scripts/verify_ingest.py

Uploads data/sample_transactions_mixed.csv (11 rows: 4 valid, 7 deliberately
wrong) to the real raw bucket and checks that the real pipeline did the right
thing with every row:

    * S3 started the ingest Lambda by itself
    * the 7 bad rows are in the quarantine bucket, each with its reasons
    * the 4 good rows are in the processed bucket
    * the 4 good rows are on the FIFO queue, grouped by account
    * each good row's journey (RECEIVED, VALIDATED, QUEUED) is in DynamoDB
    * personal information in free text was redacted before it went anywhere

Each run uses fresh transaction ids, so it can be repeated. Until the scoring
Lambda exists (Week 13 Day 5) nothing reads the queue, so this script removes
its own 4 test messages at the end.
"""

import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "eu-north-1")
PREFIX = "fintrust-capstone"
SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample_transactions_mixed.csv"
EXPECTED_VALID = 4
EXPECTED_REJECTED = 7
WAIT_SECONDS = 90

session = boto3.session.Session(region_name=REGION)
s3 = session.client("s3")
sqs = session.client("sqs")
lambda_client = session.client("lambda")
table = session.resource("dynamodb").Table(f"{PREFIX}-events")
account_id = session.client("sts").get_caller_identity()["Account"]

results = []


def check(name, passed, detail=""):
    results.append((name, bool(passed)))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def bucket(tier):
    return f"{PREFIX}-{tier}-{account_id}"


def read_object(bucket_name, key):
    try:
        return s3.get_object(Bucket=bucket_name, Key=key)["Body"].read().decode("utf-8")
    except ClientError as error:
        if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def journey(transaction_id):
    response = table.query(IndexName="by_transaction", KeyConditionExpression=Key("transaction_id").eq(transaction_id))
    return sorted((item for item in response["Items"] if item["record_type"] == "EVENT"), key=lambda e: e["at"])


def verify_configuration():
    print("\nFunction configuration")
    function = lambda_client.get_function_configuration(FunctionName=f"{PREFIX}-ingest")
    check("function is active on Python 3.13",
          function["State"] == "Active" and function["Runtime"] == "python3.13")
    check("function is outside the VPC", not function.get("VpcConfig", {}).get("VpcId"))
    check("failed invocations go to the ingest dead-letter queue",
          function.get("DeadLetterConfig", {}).get("TargetArn", "").endswith(f":{PREFIX}-ingest-dlq"))
    check("function uses its own least-privilege role", function["Role"].endswith(f"/{PREFIX}-ingest-role"))

    notification = s3.get_bucket_notification_configuration(Bucket=bucket("raw"))
    triggers = notification.get("LambdaFunctionConfigurations", [])
    check("raw bucket triggers the function for .csv objects",
          len(triggers) == 1
          and triggers[0]["LambdaFunctionArn"] == function["FunctionArn"]
          and triggers[0]["Events"] == ["s3:ObjectCreated:*"])


def verify_pipeline():
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    marker = f"TXN-V{stamp}-"
    body = SAMPLE.read_text().replace("TXN-20261005-", marker)
    key = f"uploads/verify/{datetime.now(timezone.utc):%Y-%m-%d}/mixed-{stamp}.csv"
    stem = key[:-4]

    print(f"\nUploading {SAMPLE.name} as s3://{bucket('raw')}/{key}")
    s3.put_object(Bucket=bucket("raw"), Key=key, Body=body.encode("utf-8"), ContentType="text/csv")

    print(f"Waiting for the pipeline (up to {WAIT_SECONDS} seconds)...")
    valid_ids = [f"{marker}910001", f"{marker}910005", f"{marker}910008", f"{marker}910010"]
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        stages = [[event["stage"] for event in journey(transaction_id)] for transaction_id in valid_ids]
        if all("QUEUED" in found for found in stages):
            break
        time.sleep(3)

    print("\nResults")
    stages = {transaction_id: [e["stage"] for e in journey(transaction_id)] for transaction_id in valid_ids}
    check("S3 triggered the Lambda and all 4 valid rows completed their journey",
          all(found == ["RECEIVED", "VALIDATED", "QUEUED"] for found in stages.values()),
          "; ".join(f"{tid[-6:]}: {'>'.join(found) or 'nothing'}" for tid, found in stages.items()))

    rejected_text = read_object(bucket("quarantine"), f"{stem}.rejected.csv")
    rejected = list(csv.DictReader(io.StringIO(rejected_text))) if rejected_text else []
    check("the 7 invalid rows are in the quarantine bucket", len(rejected) == EXPECTED_REJECTED,
          f"{len(rejected)} rows")
    check("every quarantined row states its row number and reason",
          bool(rejected) and all(row["row_number"] and row["errors"] for row in rejected))
    several = [row for row in rejected if row["row_number"] == "10"]
    check("a row with several problems lists all of them",
          bool(several) and several[0]["errors"].count(";") == 5)

    clean_text = read_object(bucket("processed"), f"{stem}.clean.csv")
    clean = list(csv.DictReader(io.StringIO(clean_text))) if clean_text else []
    check("the 4 valid rows are in the processed bucket", len(clean) == EXPECTED_VALID, f"{len(clean)} rows")
    flagged = {row["transaction_id"][-6:]: row["pre_flags"] for row in clean}
    check("pre-score flagged the large cross-border payment",
          flagged.get("910005") == "HIGH_AMOUNT|CROSS_BORDER", flagged.get("910005", "missing"))
    check("personal information in free text was redacted",
          bool(clean_text) and "j.naidoo" not in clean_text and "8001015009087" not in clean_text
          and "[REDACTED]" in clean_text)
    check("no invalid row reached the processed bucket",
          not any(row["transaction_id"][-6:] in ("910002", "910003", "910006", "910007", "910009") for row in clean))

    verify_queue(valid_ids)
    return key


def verify_queue(valid_ids):
    functions = [f["FunctionName"] for page in lambda_client.get_paginator("list_functions").paginate()
                 for f in page["Functions"]]
    if f"{PREFIX}-score" in functions:
        print("  [SKIP] queue contents: the scoring Lambda now consumes the queue (checked by the end-to-end test)")
        return

    queue_url = sqs.get_queue_url(QueueName=f"{PREFIX}-transactions.fifo")["QueueUrl"]
    found = {}
    deadline = time.time() + 30
    while time.time() < deadline and len(found) < len(valid_ids):
        response = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=3,
                                       VisibilityTimeout=20, AttributeNames=["MessageGroupId"])
        for message in response.get("Messages", []):
            body = json.loads(message["Body"])
            if body["transaction_id"] in valid_ids:
                found[body["transaction_id"]] = (message["Attributes"]["MessageGroupId"], body)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    check("the 4 valid rows are on the FIFO queue", len(found) == len(valid_ids), f"{len(found)} messages")
    check("each message is grouped by its account id",
          bool(found) and all(group == body["account_id"] for group, body in found.values()))
    check("each message carries its pre-score",
          bool(found) and all("pre_score" in body for _group, body in found.values()))


def main():
    print(f"Verifying the ingest Lambda in account {account_id}, region {REGION}")
    try:
        verify_configuration()
        verify_pipeline()
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        print("Look at the function's logs:  aws logs tail /aws/lambda/fintrust-capstone-ingest --since 10m",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
