"""Check that everything the live presentation needs is ready, in one run.

    python scripts/ready_check.py

Run it the morning of the presentation, and again about 15 minutes before.

It answers one question: if I start the demonstration now, will every step
work? Each line is PASS or FAIL, with what to do about a FAIL.

    * AWS CLI    signed in as the IAM user, not root
    * database   RDS available, tunnel instance running, tunnel open locally
    * pipeline   the five Lambdas active; all four queues empty, so the
                 only message the dead-letter queue shows during the talk is
                 the demo file's own
    * monitoring the CloudWatch dashboard exists; no alarm is firing
    * email      the SES sender address is verified
    * sign-in    at least one person (not the automation user) can sign in
                 as a compliance officer, so the Resolve button appears
    * reports    a monthly report is in the reports bucket, to open live
    * web        the dashboard loads over HTTPS; the API refuses a request
                 with no signed-in person (401)

It changes nothing: every AWS call reads, and it does not sign in.
(A unit test fails the build if a call that changes something is added.)
"""

import argparse
import socket
import sys
import urllib.error
import urllib.request

from botocore.exceptions import ClientError
from verify_write_api_alerts import PREFIX, REGION, TLS, account_id, api_key, check, parameter, results, session

DB_INSTANCE = f"{PREFIX}-db"
TUNNEL_NAME = f"{PREFIX}-db-tunnel"
TUNNEL_PORT = 15432
FUNCTIONS = ("ingest", "score", "alert", "query", "report")
QUEUE = f"{PREFIX}-transactions.fifo"
DLQ = f"{PREFIX}-transactions-dlq.fifo"
OTHER_DLQS = (f"{PREFIX}-ingest-dlq", f"{PREFIX}-alert-dlq")
DASHBOARD = f"{PREFIX}-operations"
REPORTS_BUCKET = f"{PREFIX}-reports-{account_id}"
OFFICER_GROUP = "compliance-officer"
AUTOMATION_USER = "fintrust-automation"

rds = session.client("rds")
ec2 = session.client("ec2")
lambda_client = session.client("lambda")
sqs = session.client("sqs")
cloudwatch = session.client("cloudwatch")
ses = session.client("ses")
cognito = session.client("cognito-idp")
s3 = session.client("s3")
sts = session.client("sts")


def waiting(queue_name):
    """(messages waiting, messages being processed) on a queue, or None if it does not exist."""
    try:
        url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    except ClientError:
        return None
    attributes = sqs.get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"]), int(attributes["ApproximateNumberOfMessagesNotVisible"])


def http_status(url, headers=None):
    """The HTTP status of a GET, or 0 if the address cannot be reached."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20, context=TLS) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0


def check_aws_cli():
    print("\nAWS CLI")
    arn = sts.get_caller_identity()["Arn"]
    check("signed in as an IAM user, not root", not arn.endswith(":root"), arn.split("/")[-1])


def check_database():
    print("\nDatabase")
    try:
        status = rds.describe_db_instances(DBInstanceIdentifier=DB_INSTANCE)["DBInstances"][0]["DBInstanceStatus"]
    except ClientError:
        status = "not found"
    check("RDS database is available", status == "available",
          status if status == "available" else f"{status}: run ./scripts/rds_start.sh (about 5 minutes)")

    reservations = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [TUNNEL_NAME]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ])["Reservations"]
    state = reservations[0]["Instances"][0]["State"]["Name"] if reservations else "not found"
    check("tunnel instance is running", state == "running",
          state if state == "running" else f"{state}: ./scripts/rds_start.sh starts it too")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        is_open = probe.connect_ex(("127.0.0.1", TUNNEL_PORT)) == 0
    check(f"database tunnel is open on localhost:{TUNNEL_PORT} (needed to onboard the account live)", is_open,
          "" if is_open else "run ./scripts/db_tunnel.sh in a second terminal window and leave it open")


def check_pipeline():
    print("\nPipeline")
    for name in FUNCTIONS:
        function = f"{PREFIX}-{name}"
        try:
            configuration = lambda_client.get_function_configuration(FunctionName=function)
            state, update = configuration.get("State"), configuration.get("LastUpdateStatus")
        except ClientError:
            state, update = "not found", ""
        check(f"Lambda {function} is active", state == "Active" and update == "Successful", f"{state} {update}".strip())

    counts = waiting(QUEUE)
    check("scoring queue is empty (nothing still being processed)", counts == (0, 0),
          "" if counts == (0, 0) else f"{counts}: wait a minute and run this again")
    for name in OTHER_DLQS:
        counts = waiting(name)
        check(f"{name} is empty", counts == (0, 0), "" if counts == (0, 0) else f"{counts}: read the runbook")

    counts = waiting(DLQ)
    check(f"{DLQ} is empty, so the demo's dead-letter row is the only one", counts == (0, 0),
          "" if counts == (0, 0) else f"{counts}: clear it as the runbook shows")


def check_monitoring():
    print("\nMonitoring")
    try:
        cloudwatch.get_dashboard(DashboardName=DASHBOARD)
        found = True
    except ClientError:
        found = False
    check(f"CloudWatch dashboard {DASHBOARD} exists", found)

    firing = {alarm["AlarmName"] for alarm in
              cloudwatch.describe_alarms(AlarmNamePrefix=f"{PREFIX}-", StateValue="ALARM")["MetricAlarms"]}
    check("no alarm is firing", not firing,
          ", ".join(sorted(firing)) + ": find out why before presenting (docs/monitoring.md)" if firing else "")


def check_email():
    print("\nEmail")
    sender = parameter("alert_sender")
    status = ses.get_identity_verification_attributes(Identities=[sender])["VerificationAttributes"]
    state = status.get(sender, {}).get("VerificationStatus", "not found")
    check("SES sender address is verified", state == "Success", state)


def check_sign_in():
    print("\nSign-in")
    pool_id = parameter("cognito_user_pool_id")
    officers = cognito.list_users_in_group(UserPoolId=pool_id, GroupName=OFFICER_GROUP)["Users"]
    people = [user["Username"] for user in officers if user["Username"] != AUTOMATION_USER and user["Enabled"]]
    check(f"at least one person is in {OFFICER_GROUP} (sign in as one of them to show Resolve)", bool(people),
          ", ".join(people) if people else "add one: python scripts/manage_users.py (see its help)")


def check_reports():
    print("\nReports")
    objects = s3.list_objects_v2(Bucket=REPORTS_BUCKET, Prefix="monthly/").get("Contents", [])
    reports = sorted((item for item in objects if item["Key"].endswith(".csv")), key=lambda item: item["LastModified"])
    check("a monthly report is in the reports bucket to open live", bool(reports),
          reports[-1]["Key"] if reports else "run: python scripts/verify_report.py")


def check_web():
    print("\nWeb")
    dashboard_url = parameter("dashboard_url")
    status = http_status(dashboard_url)
    check("dashboard loads over HTTPS", status == 200, f"{status} from {dashboard_url}")

    status = http_status(f"{parameter('api_url')}/transactions?limit=1", {"x-api-key": api_key()})
    check("API refuses a request with no signed-in person (401)", status == 401, str(status))


def main(argv=None):
    argparse.ArgumentParser(description="Check that the live presentation is ready. Changes nothing.").parse_args(argv)
    print(f"Presentation readiness check: account {account_id}, region {REGION}")
    try:
        check_aws_cli()
        check_database()
        check_pipeline()
        check_monitoring()
        check_email()
        check_sign_in()
        check_reports()
        check_web()
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("NOT READY: " + "; ".join(failed), file=sys.stderr)
        return 1
    print("READY. Next: python scripts/make_demo_file.py, then open the tabs (docs/demo-runbook.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
