#!/usr/bin/env bash
# Week 14 Day 2: monitoring and observability.
#
#   1. An SNS topic (encrypted) that emails you when an alarm fires.
#   2. Seven CloudWatch alarms:
#        scoring-error-rate      more than 5% of scoring attempts failed (5 minutes)
#        high-risk-count         5 or more HIGH risk transactions in 5 minutes
#        transactions-dlq-depth  a transaction failed scoring 3 times
#        ingest-dlq-depth        a whole file could not be processed
#        alert-dlq-depth         an alert email could not be sent
#        lambda-errors           any Lambda function crashed
#        api-5xx-errors          the API returned a server error
#      The first two use the project's own metrics in the FinTrust/ETL namespace,
#      which the Lambdas publish as log lines (see fintrust/metrics.py).
#   3. One dashboard, fintrust-capstone-operations.
#   4. Saved Logs Insights queries.
#
# Free Tier: 10 alarms, 10 custom metrics and 3 dashboards are free. This project
# uses 8 alarms (these 7 and the billing alarm), 9 custom metrics, 1 dashboard.
#
# X-Ray tracing is switched on where each resource is deployed: on every Lambda
# by deploy_function.sh and on the API stage by deploy_api.sh.
#
# Safe to run more than once.
# Usage:  ./infrastructure/monitoring/deploy_monitoring.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

HIGH_RISK_THRESHOLD="${HIGH_RISK_ALARM_THRESHOLD:-5}"     # HIGH risk transactions in 5 minutes
ERROR_RATE_THRESHOLD="${ERROR_RATE_ALARM_THRESHOLD:-5}"   # percent
TOPIC_NAME="${PREFIX}-ops-alarms"
DASHBOARD_NAME="${PREFIX}-operations"

if [[ -z "$(kms_key_arn)" ]]; then
  echo "ERROR: KMS key not found. Run ./infrastructure/kms/deploy_key.sh first." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---------- 1. where alarms send their email ----------
# create-topic returns the existing topic if it is already there.
TOPIC_ARN="$(aws sns create-topic --name "$TOPIC_NAME" \
    --attributes "KmsMasterKeyId=${KEY_ALIAS}" \
    --tags Key=Project,Value=fintrust-capstone --query TopicArn --output text)"
aws sns set-topic-attributes --topic-arn "$TOPIC_ARN" \
    --attribute-name KmsMasterKeyId --attribute-value "$KEY_ALIAS"
echo "Alarm topic  $TOPIC_NAME (encrypted with $KEY_ALIAS)"

EMAIL="$(aws ssm get-parameter --name "/fintrust/${FINTRUST_ENV}/alert_recipient" \
    --query Parameter.Value --output text)"
SUBSCRIPTION="$(aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
    --query "Subscriptions[?Endpoint=='${EMAIL}'].SubscriptionArn | [0]" --output text | none_to_empty)"
if [[ -z "$SUBSCRIPTION" ]]; then
  aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email --notification-endpoint "$EMAIL" >/dev/null
  SUBSCRIPTION="PendingConfirmation"
  echo "Subscribed   $EMAIL (confirmation email sent)"
fi

# ---------- 2. alarms ----------
# Every alarm: emails the topic when it fires AND when it recovers, and treats
# "no data" as healthy (a quiet system is not a broken one).
COMMON=(--alarm-actions "$TOPIC_ARN" --ok-actions "$TOPIC_ARN" --treat-missing-data notBreaching
        --tags Key=Project,Value=fintrust-capstone)

render "$HERE/alarm-scoring-error-rate.json" > "$WORK/error-rate.json"
aws cloudwatch put-metric-alarm --alarm-name "${PREFIX}-scoring-error-rate" \
    --alarm-description "More than ${ERROR_RATE_THRESHOLD}% of scoring attempts failed in 5 minutes. Check the score Lambda's logs and the dead-letter queue." \
    --metrics "file://$WORK/error-rate.json" \
    --comparison-operator GreaterThanThreshold --threshold "$ERROR_RATE_THRESHOLD" \
    --evaluation-periods 1 --datapoints-to-alarm 1 "${COMMON[@]}"

aws cloudwatch put-metric-alarm --alarm-name "${PREFIX}-high-risk-count" \
    --alarm-description "${HIGH_RISK_THRESHOLD} or more HIGH risk transactions were scored in 5 minutes. This can mean a fraud pattern or a bad batch; review the open alerts." \
    --namespace "FinTrust/ETL" --metric-name HighRiskTransactions \
    --dimensions "Name=Environment,Value=${FINTRUST_ENV}" \
    --statistic Sum --period 300 \
    --comparison-operator GreaterThanOrEqualToThreshold --threshold "$HIGH_RISK_THRESHOLD" \
    --evaluation-periods 1 --datapoints-to-alarm 1 "${COMMON[@]}"

dlq_alarm() {   # dlq_alarm <alarm suffix> <queue name> <description>
  aws cloudwatch put-metric-alarm --alarm-name "${PREFIX}-$1" \
      --alarm-description "$3" \
      --namespace "AWS/SQS" --metric-name ApproximateNumberOfMessagesVisible \
      --dimensions "Name=QueueName,Value=$2" \
      --statistic Maximum --period 60 \
      --comparison-operator GreaterThanThreshold --threshold 0 \
      --evaluation-periods 1 --datapoints-to-alarm 1 "${COMMON[@]}"
}
dlq_alarm transactions-dlq-depth "$DLQ_NAME" \
    "A transaction failed scoring 3 times and is waiting in the dead-letter queue. Fix the cause, then run scripts/redrive_dlq.sh."
dlq_alarm ingest-dlq-depth "$INGEST_DLQ_NAME" \
    "The ingest Lambda could not process a file after its retries. Check its logs, then upload the file again."
dlq_alarm alert-dlq-depth "$ALERT_DLQ_NAME" \
    "An alert email could not be sent after 3 retries. The alert itself is in the ledger; check SES and the alert Lambda's logs."

render "$HERE/alarm-lambda-errors.json" > "$WORK/lambda-errors.json"
aws cloudwatch put-metric-alarm --alarm-name "${PREFIX}-lambda-errors" \
    --alarm-description "A Lambda function ended with an error in the last 5 minutes. Open the dashboard's error table to see which one and why." \
    --metrics "file://$WORK/lambda-errors.json" \
    --comparison-operator GreaterThanThreshold --threshold 0 \
    --evaluation-periods 1 --datapoints-to-alarm 1 "${COMMON[@]}"

aws cloudwatch put-metric-alarm --alarm-name "${PREFIX}-api-5xx-errors" \
    --alarm-description "The API returned a server error (5xx) in the last 5 minutes. Is the database running?" \
    --namespace "AWS/ApiGateway" --metric-name 5XXError \
    --dimensions "Name=ApiName,Value=${PREFIX}-api" "Name=Stage,Value=${FINTRUST_ENV}" \
    --statistic Sum --period 300 \
    --comparison-operator GreaterThanThreshold --threshold 0 \
    --evaluation-periods 1 --datapoints-to-alarm 1 "${COMMON[@]}"
echo "Alarms       7 created or updated"

# ---------- 3. dashboard ----------
render "$HERE/dashboard.json" > "$WORK/dashboard.json"
aws cloudwatch put-dashboard --dashboard-name "$DASHBOARD_NAME" \
    --dashboard-body "file://$WORK/dashboard.json" >/dev/null
echo "Dashboard    $DASHBOARD_NAME"

# ---------- 4. saved Logs Insights queries ----------
echo "Saved queries:"
python3 "$HERE/save_queries.py"

echo
aws cloudwatch describe-alarms --alarm-name-prefix "${PREFIX}-" \
    --query 'sort_by(MetricAlarms,&AlarmName)[].{Alarm:AlarmName,State:StateValue,Threshold:Threshold}' --output table
echo "Dashboard:  https://${AWS_REGION}.console.aws.amazon.com/cloudwatch/home?region=${AWS_REGION}#dashboards/dashboard/${DASHBOARD_NAME}"

if [[ "$SUBSCRIPTION" == "PendingConfirmation" ]]; then
  echo
  echo "ACTION NEEDED: open the email from AWS Notifications with the subject"
  echo "\"AWS Notification - Subscription Confirmation\" and click \"Confirm subscription\"."
  echo "Until you do, alarms will fire but no email will be sent."
fi
