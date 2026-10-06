#!/usr/bin/env bash
# Billing safety net: email me if estimated charges pass 11 USD (about R200),
# the threshold named in the capstone brief.
#
# Billing metrics exist ONLY in us-east-1 (N. Virginia), whatever region the
# project runs in, so this one alarm is created there.
#
# Before running, as the ROOT user in the console (one time only):
#   Billing and Cost Management > Billing preferences > Alert preferences > Edit
#   > tick "CloudWatch billing alerts" > Update
#
# Usage:  ALERT_EMAIL=you@example.com ./infrastructure/billing/create_billing_alarm.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

if [[ -z "${ALERT_EMAIL:-}" ]]; then
  echo "ERROR: set ALERT_EMAIL, e.g.  ALERT_EMAIL=you@example.com $0" >&2
  exit 1
fi

BILLING_REGION="us-east-1"
THRESHOLD_USD="${THRESHOLD_USD:-11}"

TOPIC_ARN="$(aws sns create-topic --region "$BILLING_REGION" \
    --name "${PREFIX}-billing-alerts" --query TopicArn --output text)"
aws sns subscribe --region "$BILLING_REGION" --topic-arn "$TOPIC_ARN" \
    --protocol email --notification-endpoint "$ALERT_EMAIL" >/dev/null

aws cloudwatch put-metric-alarm --region "$BILLING_REGION" \
    --alarm-name "${PREFIX}-billing-over-${THRESHOLD_USD}-usd" \
    --alarm-description "Estimated AWS charges are above ${THRESHOLD_USD} USD (about R200)" \
    --namespace "AWS/Billing" --metric-name EstimatedCharges \
    --dimensions Name=Currency,Value=USD \
    --statistic Maximum --period 21600 --evaluation-periods 1 \
    --threshold "$THRESHOLD_USD" --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$TOPIC_ARN"

echo "Billing alarm created in ${BILLING_REGION}."
echo "IMPORTANT: open the email from AWS Notifications and click 'Confirm subscription'."
