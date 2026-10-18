"""Verify Week 14 Day 2 on the deployed account: monitoring and observability.

    python scripts/verify_monitoring.py

The database must be running (./scripts/rds_start.sh).

Part 1 checks the configuration: alarm topic, 7 alarms, dashboard, saved
queries, X-Ray tracing and log retention.

Part 2 proves it works with real traffic: it uploads 15 transactions through
the API and then checks that
    * the custom metrics in FinTrust/ETL show exactly what happened
    * Logs Insights can find the file by its name with its row counts
    * X-Ray recorded traces
    * an alarm really can send an email (it is set to ALARM for a moment)

CloudWatch needs a minute or two to show new metrics, so Part 2 takes a while.
Each run adds 15 sample transactions and sends 2 alert emails and 2 alarm emails.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.exceptions import ClientError
from verify_write_api_alerts import PREFIX, REGION, SAMPLE, account_id, api_settings, call, check, results, session

NAMESPACE = "FinTrust/ETL"
DIMENSIONS = [{"Name": "Environment", "Value": "dev"}]
TOPIC_NAME = f"{PREFIX}-ops-alarms"
DASHBOARD_NAME = f"{PREFIX}-operations"
FUNCTIONS = ("ingest", "score", "alert", "query", "report")
ALARMS = (
    "scoring-error-rate", "high-risk-count", "transactions-dlq-depth", "ingest-dlq-depth",
    "alert-dlq-depth", "lambda-errors", "api-5xx-errors",
)
QUERIES_FILE = Path(__file__).resolve().parents[1] / "infrastructure" / "monitoring" / "queries.json"
SAVED_QUERIES = len(json.loads(QUERIES_FILE.read_text()))        # every query in the file is saved
WAIT_SECONDS = 420

cloudwatch = session.client("cloudwatch")
logs = session.client("logs")
sns = session.client("sns")
kms = session.client("kms")
xray = session.client("xray")
lambda_client = session.client("lambda")
apigateway = session.client("apigateway")

TOPIC_ARN = f"arn:aws:sns:{REGION}:{account_id}:{TOPIC_NAME}"


def verify_topic():
    print("\nAlarm notifications")
    try:
        attributes = sns.get_topic_attributes(TopicArn=TOPIC_ARN)["Attributes"]
    except ClientError:
        check("the alarm topic exists", False, "run ./infrastructure/monitoring/deploy_monitoring.sh")
        return False
    check("the alarm topic is encrypted with the project key", attributes.get("KmsMasterKeyId") == f"alias/{PREFIX}")
    subscriptions = sns.list_subscriptions_by_topic(TopicArn=TOPIC_ARN)["Subscriptions"]
    confirmed = [s for s in subscriptions if s["Protocol"] == "email" and s["SubscriptionArn"].startswith("arn:")]
    check("an email address is subscribed and confirmed", len(confirmed) >= 1,
          "" if confirmed else "click 'Confirm subscription' in the email from AWS Notifications")
    # get_key_policy does not accept an alias, so look up the key id first.
    key_id = kms.describe_key(KeyId=f"alias/{PREFIX}")["KeyMetadata"]["KeyId"]
    policy = json.loads(kms.get_key_policy(KeyId=key_id, PolicyName="default")["Policy"])
    services = [s.get("Principal", {}).get("Service") for s in policy["Statement"]]
    check("the key policy lets CloudWatch publish to the encrypted topic", "cloudwatch.amazonaws.com" in services)
    return bool(confirmed)


def verify_alarms():
    print("\nAlarms")
    found = {a["AlarmName"]: a for a in cloudwatch.describe_alarms(AlarmNamePrefix=f"{PREFIX}-")["MetricAlarms"]}
    missing = [name for name in ALARMS if f"{PREFIX}-{name}" not in found]
    check("all 7 alarms exist", not missing, "missing: " + ", ".join(missing) if missing else "")
    if missing:
        return
    mine = [found[f"{PREFIX}-{name}"] for name in ALARMS]
    check("every alarm emails the topic when it fires and when it recovers",
          all(a["AlarmActions"] == [TOPIC_ARN] and a["OKActions"] == [TOPIC_ARN] for a in mine))
    check("a quiet system is treated as healthy (missing data is not breaching)",
          all(a["TreatMissingData"] == "notBreaching" for a in mine))

    high = found[f"{PREFIX}-high-risk-count"]
    check("high-risk-count watches HighRiskTransactions in FinTrust/ETL",
          high.get("Namespace") == NAMESPACE and high.get("MetricName") == "HighRiskTransactions"
          and high["Statistic"] == "Sum" and high["Period"] == 300, f"threshold {high['Threshold']:.0f} in 5 minutes")
    rate = found[f"{PREFIX}-scoring-error-rate"]
    expression = next((m["Expression"] for m in rate.get("Metrics", []) if m.get("ReturnData")), "")
    names = {m["MetricStat"]["Metric"]["MetricName"] for m in rate.get("Metrics", []) if "MetricStat" in m}
    check("scoring-error-rate is failures divided by all attempts, as a percentage",
          names == {"ScoringFailures", "TransactionsScored"} and "100 * failures / (failures + scored)" in expression,
          f"threshold {rate['Threshold']:.0f}%")
    queues = {a["Dimensions"][0]["Value"] for a in mine if a.get("Namespace") == "AWS/SQS"}
    check("each of the three dead-letter queues has its own alarm",
          queues == {f"{PREFIX}-transactions-dlq.fifo", f"{PREFIX}-ingest-dlq", f"{PREFIX}-alert-dlq"})
    total = len(found)
    check("the number of alarms stays inside the Free Tier (10)", total + 1 <= 10, f"{total} here + 1 billing alarm")


def verify_dashboard_and_queries():
    print("\nDashboard and saved queries")
    try:
        body = json.loads(cloudwatch.get_dashboard(DashboardName=DASHBOARD_NAME)["DashboardBody"])
    except ClientError:
        check("the dashboard exists", False)
        body = {"widgets": []}
    text = json.dumps(body)
    check("the dashboard exists and charts the FinTrust/ETL metrics",
          len(body["widgets"]) >= 10 and NAMESPACE in text and "HighRiskTransactions" in text,
          f"{len(body['widgets'])} widgets")
    check("the dashboard shows alarm status, queue depth and an error table",
          any(w["type"] == "alarm" for w in body["widgets"]) and any(w["type"] == "log" for w in body["widgets"])
          and "ApproximateNumberOfMessagesVisible" in text)
    # The prefix may not end with "/", so ask for "FinTrust" and keep "FinTrust/..." names.
    saved = [q for q in logs.describe_query_definitions(queryDefinitionNamePrefix="FinTrust")["queryDefinitions"]
             if q["name"].startswith("FinTrust/")]
    check(f"{SAVED_QUERIES} Logs Insights queries are saved under FinTrust/", len(saved) == SAVED_QUERIES,
          f"{len(saved)} found")
    check("one of them is the failed-ingestion query", any("Failed ingestion" in q["name"] for q in saved))


def verify_tracing_and_logs():
    print("\nX-Ray tracing and log retention")
    modes = {f: lambda_client.get_function_configuration(FunctionName=f"{PREFIX}-{f}")["TracingConfig"]["Mode"]
             for f in FUNCTIONS}
    check("X-Ray active tracing is on for all five Lambda functions", set(modes.values()) == {"Active"}, str(modes))
    api_id = next(a["id"] for a in apigateway.get_rest_apis(limit=500)["items"] if a["name"] == f"{PREFIX}-api")
    stage = apigateway.get_stage(restApiId=api_id, stageName="dev")
    check("X-Ray tracing is on for the API stage", stage.get("tracingEnabled") is True)
    groups = {g["logGroupName"]: g.get("retentionInDays")
              for g in logs.describe_log_groups(logGroupNamePrefix=f"/aws/lambda/{PREFIX}-")["logGroups"]}
    check("each function's logs are kept for 30 days, not forever",
          all(groups.get(f"/aws/lambda/{PREFIX}-{f}") == 30 for f in FUNCTIONS))


def metric_sum(name, since):
    points = cloudwatch.get_metric_statistics(
        Namespace=NAMESPACE, MetricName=name, Dimensions=DIMENSIONS, StartTime=since,
        EndTime=datetime.now(timezone.utc) + timedelta(minutes=1), Period=60, Statistics=["Sum"],
    )["Datapoints"]
    return int(sum(point["Sum"] for point in points))


def insights_rows(key, since):
    """Run a Logs Insights query for one file's summary line. Returns the result rows."""
    query = logs.start_query(
        logGroupName=f"/aws/lambda/{PREFIX}-ingest",
        startTime=int(since.timestamp()), endTime=int(time.time()) + 60,
        queryString=("fields source_key, rows_read, rows_queued, rows_quarantined "
                     f'| filter message = "file_processed" and source_key = "{key}"'),
    )["queryId"]
    for _ in range(30):
        result = logs.get_query_results(queryId=query)
        if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            return [{field["field"]: field["value"] for field in row} for row in result.get("results", [])]
        time.sleep(1)
    return []


def traced_services(since):
    graph = xray.get_service_graph(StartTime=since, EndTime=datetime.now(timezone.utc))
    return sorted({s.get("Name", "") for s in graph.get("Services", []) if PREFIX in s.get("Name", "")})


def verify_live(url, key):
    since = datetime.now(timezone.utc) - timedelta(seconds=30)
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    body = SAMPLE.read_text().replace("-9000", f"-{stamp}")

    print("\nReal traffic")
    status, accepted = call(url, "POST", "/upload", key, {"filename": f"monitoring-{stamp}.csv"}, body, "text/csv")
    check("15 transactions uploaded through the API", status == 202, f"HTTP {status}")
    if status != 202:
        return
    object_key = accepted["object_key"]

    expected = {"RowsRead": 15, "RowsQueued": 15, "RowsQuarantined": 0, "TransactionsScored": 15,
                "HighRiskTransactions": 2, "AlertsSent": 2}
    print(f"Waiting for CloudWatch to show the metrics (up to {WAIT_SECONDS // 60} minutes)...")
    seen, rows, services = {}, [], []
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        seen = {name: metric_sum(name, since) for name in expected}
        if not rows:
            rows = insights_rows(object_key, since)
        services = traced_services(since)
        if all(seen[name] >= value for name, value in expected.items()) and rows and len(services) >= 2:
            break
        time.sleep(15)

    print("\nCustom metrics (namespace FinTrust/ETL)")
    for name, value in expected.items():
        # ">=" because another upload in the same few minutes would add to the totals.
        check(f"{name} shows {value}", seen.get(name, 0) >= value and (value > 0 or seen.get(name, 0) == 0),
              f"CloudWatch has {seen.get(name, 0)}")
    check("rows read = rows queued + rows quarantined (nothing lost between steps)",
          seen.get("RowsRead", 0) > 0
          and seen.get("RowsRead") == seen.get("RowsQueued", 0) + seen.get("RowsQuarantined", 0))

    print("\nLogs Insights and X-Ray")
    check("Logs Insights finds the file by name with its row counts",
          len(rows) == 1 and rows[0].get("rows_read") == "15" and rows[0].get("rows_queued") == "15", object_key)
    check("X-Ray recorded traces for the API and the Lambda functions", len(services) >= 2, ", ".join(services))


def verify_notification():
    print("\nAlarm email")
    name = f"{PREFIX}-high-risk-count"
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    cloudwatch.set_alarm_state(AlarmName=name, StateValue="ALARM",
                               StateReason="Test by scripts/verify_monitoring.py: checking that alarm emails work")
    outcome = ""
    for _ in range(20):
        time.sleep(3)
        history = cloudwatch.describe_alarm_history(AlarmName=name, HistoryItemType="Action", StartDate=started)
        summaries = [item["HistorySummary"] for item in history["AlarmHistoryItems"]]
        if summaries:
            outcome = summaries[-1]
            break
    check("the alarm published to the encrypted topic when it fired", outcome.startswith("Successfully executed"),
          outcome or "no action recorded")
    print(f"  The alarm returns to OK by itself within a few minutes.\n"
          f"  Check your inbox for an email whose subject starts with:  ALARM: \"{name}\"")


def main():
    print(f"Verifying monitoring in account {account_id}, region {REGION}")
    try:
        confirmed = verify_topic()
        verify_alarms()
        verify_dashboard_and_queries()
        verify_tracing_and_logs()

        url, key = api_settings()
        status, health = call(url, "GET", "/health", key)
        if status != 200:
            check("the API can reach the database", False, f"HTTP {status} {health}")
            print("\nStopping: is the database running?  ./scripts/rds_start.sh")
        else:
            verify_live(url, key)
            if confirmed:
                verify_notification()
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
