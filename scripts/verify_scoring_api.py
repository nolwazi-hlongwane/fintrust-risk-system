"""Verify Week 13 Day 5 on the deployed account: scoring and the read API.

    python scripts/verify_scoring_api.py

The database must be running (./scripts/rds_start.sh).

Uploads data/sample_transactions_valid.csv (15 transactions, with fresh ids) to
the real raw bucket, then checks, ONLY through the public API, that:

    * API Gateway rejects a request without the API key
    * the query Lambda can log in to the private database with its IAM token
    * all 15 transactions were ingested, queued, scored and stored in the ledger
    * scores and rule breakdowns are right, and DynamoDB agrees with RDS
    * filtering, pagination, insights and error responses behave as designed

Each run adds 15 sample transactions (dated 1 to 5 October 2026) to the ledger.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3
from api_auth import headers_for, sign_in
from botocore.exceptions import ClientError

try:
    # Python installed from python.org on macOS does not use the system's trusted
    # certificates, so HTTPS fails with CERTIFICATE_VERIFY_FAILED. certifi (already
    # installed with boto3's dependencies) provides the same trusted list.
    import certifi

    TLS = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    TLS = ssl.create_default_context()

REGION = os.environ.get("AWS_REGION", "eu-north-1")
PREFIX = "fintrust-capstone"
SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample_transactions_valid.csv"
WAIT_SECONDS = 180

session = boto3.session.Session(region_name=REGION)
s3 = session.client("s3")
lambda_client = session.client("lambda")
apigateway = session.client("apigateway")
account_id = session.client("sts").get_caller_identity()["Account"]

results = []


def check(name, passed, detail=""):
    results.append((name, bool(passed)))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def api_settings():
    url = session.client("ssm").get_parameter(Name="/fintrust/dev/api_url")["Parameter"]["Value"]
    key_id = apigateway.get_api_keys(nameQuery=f"{PREFIX}-compliance-key")["items"][0]["id"]
    key = apigateway.get_api_key(apiKey=key_id, includeValue=True)["value"]
    return url, sign_in(session, url, key)        # signed in as the automation user (Week 15 Day 2)


def call(url, path, params=None, key=None):
    """GET a route. Returns (status, body as dict, or None if the body is not JSON)."""
    full = f"{url}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
    request = urllib.request.Request(full, headers=headers_for(key))
    try:
        with urllib.request.urlopen(request, timeout=30, context=TLS) as response:
            status, text = response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as error:
        return 0, {"network_error": str(error)}     # could not reach the API at all
    try:
        return status, json.loads(text)
    except ValueError:
        return status, None


def verify_configuration():
    print("\nConfiguration")
    for name in ("score", "query"):
        function = lambda_client.get_function_configuration(FunctionName=f"{PREFIX}-{name}")
        vpc = function.get("VpcConfig", {})
        check(f"{name} Lambda is active inside the VPC, in two subnets",
              function["State"] == "Active" and bool(vpc.get("VpcId")) and len(vpc.get("SubnetIds", [])) == 2)
        variables = function.get("Environment", {}).get("Variables", {})
        check(f"{name} Lambda logs in as its own database user with an IAM token, and has no password",
              variables.get("FINTRUST_DB_USER") == f"fintrust_{name}"
              and variables.get("FINTRUST_DB_AUTH_MODE") == "iam"
              and not any("PASSWORD" in key.upper() for key in variables))

    mappings = lambda_client.list_event_source_mappings(FunctionName=f"{PREFIX}-score")["EventSourceMappings"]
    check("the FIFO queue is connected to the score Lambda and reports failures per message",
          len(mappings) == 1 and mappings[0]["State"] in ("Enabled", "Enabling", "Creating", "Updating")
          and mappings[0]["EventSourceArn"].endswith(f":{PREFIX}-transactions.fifo")
          and mappings[0].get("FunctionResponseTypes") == ["ReportBatchItemFailures"])


def verify_access(url, key):
    print("\nAPI access")
    status, _body = call(url, "/health")
    check("a request with no API key is refused by API Gateway", status == 403, f"HTTP {status}")
    status, _body = call(url, "/health", key="not-the-real-key")
    check("a request with a wrong API key is refused", status == 403, f"HTTP {status}")
    # A new API key can take up to a minute to become active, and the first call
    # to a Lambda in a VPC is slower, so allow a few attempts.
    status, body = 0, None
    for _attempt in range(12):
        status, body = call(url, "/health", key=key)
        if status == 200:
            break
        time.sleep(5)
    healthy = status == 200 and (body or {}).get("database") == "ok"
    check("with the key, /health reports the database is reachable", healthy, f"HTTP {status} {body}")
    return healthy


def verify_pipeline(url, key):
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    body = SAMPLE.read_text().replace("-9000", f"-{stamp}")
    ids = [line.split(",")[0] for line in body.strip().splitlines()[1:]]
    object_key = f"uploads/verify/{datetime.now(timezone.utc):%Y-%m-%d}/valid-{stamp}.csv"

    print(f"\nUploading {SAMPLE.name} ({len(ids)} transactions) as {object_key}")
    s3.put_object(Bucket=f"{PREFIX}-raw-{account_id}", Key=object_key, Body=body.encode("utf-8"),
                  ContentType="text/csv")
    print(f"Waiting for scoring (up to {WAIT_SECONDS} seconds)...")
    journeys = {}
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        for transaction_id in ids:
            if journeys.get(transaction_id, {}).get("status") not in ("SCORED", "ALERTED"):
                status, found = call(url, f"/transactions/{transaction_id}/journey", key=key)
                journeys[transaction_id] = found if status == 200 else {}
        if all(journey.get("status") in ("SCORED", "ALERTED") for journey in journeys.values()):
            break
        time.sleep(4)

    print("\nPipeline")
    scored = [tid for tid, journey in journeys.items() if journey.get("status") in ("SCORED", "ALERTED")]
    check("all 15 transactions were ingested, queued and scored", len(scored) == len(ids),
          f"{len(scored)} of {len(ids)}")

    high_id, medium_id, low_id = ids[6], ids[8], ids[0]      # 88 750 to AE; 75 000 in ZA; 412.50 groceries
    high = journeys.get(high_id) or {}
    check("the large cross-border payment scored 70, HIGH",
          (high.get("score") or {}).get("risk_score") == 70 and (high.get("score") or {}).get("risk_level") == "HIGH")
    check("its score explains itself: HIGH_AMOUNT 50 + CROSS_BORDER 20",
          [(rule["rule"], rule["points"]) for rule in (high.get("score") or {}).get("rules", [])]
          == [("HIGH_AMOUNT", 50), ("CROSS_BORDER", 20)])
    stages = [event["stage"] for event in high.get("events", [])]
    check("its journey shows every step in order",
          [stage for stage in stages if stage != "ALERT_SENT"] == ["RECEIVED", "VALIDATED", "QUEUED", "SCORED"],
          " > ".join(stages))
    medium = (journeys.get(medium_id) or {}).get("score") or {}
    check("the large domestic payment scored 50, MEDIUM",
          (medium.get("risk_score"), medium.get("risk_level")) == (50, "MEDIUM"))
    low = (journeys.get(low_id) or {}).get("score") or {}
    check("an ordinary purchase scored 0, LOW with no rules", (low.get("risk_score"), low.get("rules")) == (0, []))

    print("\nRead API")
    status, found = call(url, "/transactions", {"transaction_id": high_id}, key)
    row = (found or {}).get("items", [{}])[0] if (found or {}).get("items") else {}
    check("the ledger (RDS) agrees with the event store (DynamoDB)",
          status == 200 and row.get("risk_score") == 70 and row.get("risk_level") == "HIGH"
          and row.get("amount") == "88750.00", f"HTTP {status}")

    status, found = call(url, "/transactions", {"account_id": "ACC-1001", "limit": "5"}, key)
    items = (found or {}).get("items", [])
    check("filter by account, with pagination",
          status == 200 and 0 < len(items) <= 5 and {item["account_id"] for item in items} == {"ACC-1001"}
          and found["pagination"]["limit"] == 5 and found["pagination"]["total"] >= len(items))

    status, found = call(url, "/transactions", {"risk_level": "HIGH", "limit": "100"}, key)
    check("filter by risk level",
          status == 200 and found["items"] and all(item["risk_level"] == "HIGH" for item in found["items"]))

    status, insights = call(url, "/insights", key=key)
    check("insights: the three risk levels add up to the total",
          status == 200 and sum(insights["by_risk_level"].values()) == insights["totals"]["transactions"],
          f"{insights.get('totals', {}).get('transactions')} transactions" if insights else f"HTTP {status}")

    status, found = call(url, "/transactions", {"limit": "500"}, key)
    check("a bad parameter is a clear 400, not a server error",
          status == 400 and found["error"]["code"] == "VALIDATION_ERROR", f"HTTP {status}")
    status, found = call(url, "/transactions/TXN-DOES-NOT-EXIST/journey", key=key)
    check("an unknown transaction is a 404", status == 404 and found["error"]["code"] == "NOT_FOUND", f"HTTP {status}")
    status, found = call(url, "/transactions", {"account_id": "ACC-1001'; DROP TABLE alerts; --"}, key)
    check("an injection attempt is rejected before reaching the database", status == 400, f"HTTP {status}")


def main():
    print(f"Verifying scoring and the read API in account {account_id}, region {REGION}")
    try:
        verify_configuration()
        url, key = api_settings()
        print(f"\nAPI: {url}")
        if verify_access(url, key):
            verify_pipeline(url, key)
        else:
            print("\nStopping: the API cannot reach the database. Is it running?  ./scripts/rds_start.sh")
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        print("Logs:  aws logs tail /aws/lambda/fintrust-capstone-score --since 10m", file=sys.stderr)
        print("       aws logs tail /aws/lambda/fintrust-capstone-query --since 10m", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
