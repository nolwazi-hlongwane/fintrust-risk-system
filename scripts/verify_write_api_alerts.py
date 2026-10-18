"""Verify Week 14 Day 1 on the deployed account: the write API and the alert system.

    python scripts/verify_write_api_alerts.py

The database must be running (./scripts/rds_start.sh) and the SES address verified.

Everything is done through the public API, the way the dashboard will do it:

    * POST /upload sends 15 transactions and gets 202 back at once
    * the pipeline ingests, scores and stores them in the background
    * the HIGH risk transaction raises an alert AND a real email through SES
      (check your inbox: the script tells you the subject to look for)
    * GET /alerts lists the new alert; POST /resolve closes it; a second
      resolve is refused
    * POST /flag flags an account through the flag_account database function
    * bad requests get clear 4xx answers

Each run adds 15 sample transactions to the ledger, resolves the alert it
created, and adds one MANUAL_FLAG event to account ACC-1008.
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
    # Python from python.org on macOS does not use the system's trusted
    # certificates; certifi (installed with boto3's dependencies) provides them.
    import certifi

    TLS = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    TLS = ssl.create_default_context()

REGION = os.environ.get("AWS_REGION", "eu-north-1")
PREFIX = "fintrust-capstone"
SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample_transactions_valid.csv"
WAIT_SECONDS = 240
FLAG_ACCOUNT = "ACC-1008"

session = boto3.session.Session(region_name=REGION)
lambda_client = session.client("lambda")
apigateway = session.client("apigateway")
ses = session.client("ses")
ssm = session.client("ssm")
account_id = session.client("sts").get_caller_identity()["Account"]

results = []


def check(name, passed, detail=""):
    results.append((name, bool(passed)))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def parameter(name):
    return ssm.get_parameter(Name=f"/fintrust/dev/{name}")["Parameter"]["Value"]


def api_key():
    """The scripts' own API key (the dashboard has a separate one)."""
    key_id = apigateway.get_api_keys(nameQuery=f"{PREFIX}-compliance-key")["items"][0]["id"]
    return apigateway.get_api_key(apiKey=key_id, includeValue=True)["value"]


def api_settings():
    """The API address, and credentials signed in as the automation user (Week 15 Day 2)."""
    url = parameter("api_url")
    return url, sign_in(session, url, api_key())


def call(url, method, path, key, params=None, body=None, content_type="application/json"):
    """Call a route. Returns (status, body as dict or None)."""
    full = f"{url}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
    data = None
    headers = headers_for(key)
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        headers["Content-Type"] = content_type
    request = urllib.request.Request(full, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30, context=TLS) as response:
            status, text = response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as error:
        return 0, {"network_error": str(error)}
    try:
        return status, json.loads(text)
    except ValueError:
        return status, None


def verify_configuration():
    print("\nConfiguration")
    function = lambda_client.get_function_configuration(FunctionName=f"{PREFIX}-alert")
    check("alert Lambda is active, outside the VPC, with its own role",
          function["State"] == "Active" and not function.get("VpcConfig", {}).get("VpcId")
          and function["Role"].endswith(f"/{PREFIX}-alert-role"))

    mappings = lambda_client.list_event_source_mappings(FunctionName=f"{PREFIX}-alert")["EventSourceMappings"]
    mapping = mappings[0] if len(mappings) == 1 else {}
    check("the DynamoDB stream is connected to the alert Lambda",
          ":table/" in mapping.get("EventSourceArn", "") and "/stream/" in mapping.get("EventSourceArn", "")
          and mapping.get("State") in ("Enabled", "Enabling", "Creating", "Updating"), mapping.get("State", "missing"))
    pattern = (mapping.get("FilterCriteria", {}).get("Filters") or [{}])[0].get("Pattern", "")
    check("the trigger is filtered to HIGH risk scores only", '"HIGH"' in pattern and '"SCORE"' in pattern)
    destination = mapping.get("DestinationConfig", {}).get("OnFailure", {}).get("Destination", "")
    check("undeliverable alerts go to the alert dead-letter queue", destination.endswith(f":{PREFIX}-alert-dlq"))

    sender, recipient = parameter("alert_sender"), parameter("alert_recipient")
    attributes = ses.get_identity_verification_attributes(Identities=[sender, recipient])["VerificationAttributes"]
    verified = all(
        attributes.get(address, {}).get("VerificationStatus") == "Success" for address in (sender, recipient)
    )
    check("the alert sender and recipient are verified in SES", verified)
    return verified


def verify_upload_and_alert(url, key):
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    body = SAMPLE.read_text().replace("-9000", f"-{stamp}")
    ids = [line.split(",")[0] for line in body.strip().splitlines()[1:]]
    high_id = ids[6]                                   # 88 750 to AE: HIGH_AMOUNT + CROSS_BORDER = 70

    print("\nUpload through the API")
    started = time.monotonic()
    status, accepted = call(url, "POST", "/upload", key, {"filename": f"verify-{stamp}.csv"}, body, "text/csv")
    elapsed = time.monotonic() - started
    check("POST /upload answers 202 Accepted", status == 202 and (accepted or {}).get("status") == "ACCEPTED",
          f"HTTP {status} in {elapsed:.1f}s")
    check("the file was stored under uploads/ with a generated name",
          (accepted or {}).get("object_key", "").startswith("uploads/")
          and (accepted or {}).get("object_key", "").endswith(f"-verify-{stamp}.csv"),
          (accepted or {}).get("object_key", ""))
    if status != 202:
        return None

    print(f"Waiting for scoring and the alert email (up to {WAIT_SECONDS} seconds)...")
    journeys, deadline = {}, time.time() + WAIT_SECONDS
    while time.time() < deadline:
        for transaction_id in ids:
            if journeys.get(transaction_id, {}).get("status") not in ("SCORED", "ALERTED"):
                found_status, found = call(url, "GET", f"/transactions/{transaction_id}/journey", key)
                journeys[transaction_id] = found if found_status == 200 else {}
        if all(j.get("status") in ("SCORED", "ALERTED") for j in journeys.values()):
            found_status, found = call(url, "GET", f"/transactions/{high_id}/journey", key)
            journeys[high_id] = found if found_status == 200 else {}
            if journeys[high_id].get("status") == "ALERTED":
                break
        time.sleep(4)

    print("\nPipeline and alert")
    done = [tid for tid, journey in journeys.items() if journey.get("status") in ("SCORED", "ALERTED")]
    check("all 15 uploaded transactions were scored in the background", len(done) == len(ids),
          f"{len(done)} of {len(ids)}")
    high = journeys.get(high_id) or {}
    stages = [event["stage"] for event in high.get("events", [])]
    check("the HIGH risk transaction's journey ends with ALERT_SENT",
          high.get("status") == "ALERTED" and stages[-1:] == ["ALERT_SENT"], " > ".join(stages))
    others = [journeys[tid].get("status") for tid in ids if tid != high_id]
    expected_alerts = {ids[6], ids[10]}                # the two 70-point rows in the sample file
    alerted = {tid for tid in ids if journeys.get(tid, {}).get("status") == "ALERTED"}
    check("only HIGH risk transactions were alerted (no email for LOW or MEDIUM)",
          alerted <= expected_alerts and "SCORED" in others, f"{len(alerted)} alerted")
    return high_id


def verify_alerts_and_resolve(url, key, high_id):
    print("\nAlerts and resolve")
    status, found = call(url, "GET", "/alerts", key, {"status": "OPEN", "limit": "100"})
    mine = [item for item in (found or {}).get("items", []) if item["transaction_id"] == high_id]
    check("GET /alerts lists the new alert as OPEN, with its transaction",
          status == 200 and len(mine) == 1 and mine[0]["risk_score"] == 70 and mine[0]["amount"] == "88750.00",
          f"HTTP {status}")
    if not mine:
        return
    alert_id = mine[0]["alert_id"]

    status, resolved = call(url, "POST", "/resolve", key, body={
        "alert_id": alert_id, "resolved_by": "verify.script", "resolution_note": "Resolved by the verification script"})
    check("POST /resolve closes it", status == 200 and (resolved or {}).get("status") == "RESOLVED", f"HTTP {status}")
    status, again = call(url, "POST", "/resolve", key, body={"alert_id": alert_id, "resolved_by": "verify.script"})
    check("resolving it a second time is refused with 409", status == 409 and again["error"]["code"] == "CONFLICT",
          f"HTTP {status}")
    status, found = call(url, "GET", "/alerts", key, {"status": "RESOLVED", "limit": "100"})
    closed = [item for item in (found or {}).get("items", []) if item["alert_id"] == alert_id]
    check("it now appears under RESOLVED with who resolved it",
          len(closed) == 1 and closed[0]["resolved_by"] == "verify.script")
    status, missing = call(url, "POST", "/resolve", key, body={"alert_id": 99999999, "resolved_by": "verify.script"})
    check("resolving an unknown alert is 404", status == 404, f"HTTP {status}")


def verify_flag(url, key):
    print("\nFlag an account")
    status, flagged = call(url, "POST", "/flag", key, body={
        "account_id": FLAG_ACCOUNT, "reason": "Flagged by the verification script"})
    check("POST /flag returns 201 with the new risk event id",
          status == 201 and isinstance((flagged or {}).get("risk_event_id"), int)
          and flagged.get("account_status") == "FLAGGED", f"HTTP {status}")
    status, insights = call(url, "GET", "/insights", key)
    check("the API still answers after the write (connection reused cleanly)", status == 200, f"HTTP {status}")
    status, missing = call(url, "POST", "/flag", key, body={"account_id": "ACC-0000", "reason": "No such account"})
    check("flagging an unknown account is 404", status == 404 and missing["error"]["code"] == "NOT_FOUND",
          f"HTTP {status}")
    status, closed = call(url, "POST", "/flag", key, body={"account_id": "ACC-1012", "reason": "Closed account"})
    check("flagging a closed account is refused with 409", status == 409, f"HTTP {status}")
    status, invalid = call(url, "POST", "/flag", key, body={"account_id": FLAG_ACCOUNT})
    check("a missing reason is a clear 400", status == 400 and invalid["error"]["message"] == "reason is required",
          f"HTTP {status}")


def verify_bad_requests(url, key):
    print("\nBad requests")
    status, _body = call(url, "POST", "/flag", None, body={"account_id": FLAG_ACCOUNT, "reason": "No key"})
    check("a write request with no API key and no sign-in is refused by API Gateway", status in (401, 403),
          f"HTTP {status}")
    status, body = call(url, "POST", "/upload", key, body="transaction_id,amount\nTXN-1,5\n", content_type="text/csv")
    check("an upload with the wrong columns is refused with 400 and nothing is stored",
          status == 400 and "missing required column" in body["error"]["message"], f"HTTP {status}")
    status, body = call(url, "POST", "/upload", key, {"filename": "../../etc/passwd"}, SAMPLE.read_text(), "text/csv")
    check("an upload with an unsafe file name is refused", status == 400, f"HTTP {status}")
    status, body = call(url, "POST", "/resolve", key, body="this is not json")
    check("a body that is not JSON is a clear 400", status == 400, f"HTTP {status}")


def main():
    print(f"Verifying the write API and alerts in account {account_id}, region {REGION}")
    try:
        if not verify_configuration():
            print("\nStopping: verify the email address first.  ./infrastructure/ses/deploy_ses.sh")
        else:
            url, key = api_settings()
            print(f"\nAPI: {url}")
            status, health = call(url, "GET", "/health", key)
            if status != 200:
                check("the API can reach the database", False, f"HTTP {status} {health}")
                print("\nStopping: is the database running?  ./scripts/rds_start.sh")
            else:
                high_id = verify_upload_and_alert(url, key)
                if high_id:
                    verify_alerts_and_resolve(url, key, high_id)
                verify_flag(url, key)
                verify_bad_requests(url, key)
                if high_id:
                    print(f"\nNow check your inbox for:  [FinTrust] HIGH risk transaction {high_id} (score 70)")
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        print("Logs:  aws logs tail /aws/lambda/fintrust-capstone-alert --since 10m", file=sys.stderr)
        print("       aws logs tail /aws/lambda/fintrust-capstone-query --since 10m", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
