#!/usr/bin/env bash
# Week 14 Day 4: run the report Lambda automatically every month.
#
# Creates an EventBridge rule with a cron schedule and points it at the report
# Lambda. At 06:00 South African time on the 1st of each month, EventBridge
# invokes the function, which reports on the month that has just ended.
#
# The schedule, read left to right:   cron(0 4 1 * ? *)
#     0    minute 0
#     4    hour 04 UTC = 06:00 in South Africa (UTC+2, no daylight saving)
#     1    day 1 of the month
#     *    every month
#     ?    any day of the week (AWS requires "?" in one of the two day fields)
#     *    every year
# EventBridge rule schedules are always in UTC.
#
# The Lambda's resource policy is what lets EventBridge invoke it. It names this
# one rule, so no other rule or account can start the function.
#
# If an invocation fails, Lambda retries it twice by itself (asynchronous
# invocation), and the lambda-errors alarm sends an email.
#
# Cost: EventBridge scheduled rules are free.
# Safe to run more than once.
# Usage:  ./infrastructure/eventbridge/deploy_schedule.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

RULE_NAME="${PREFIX}-monthly-report"
FUNCTION_NAME="${PREFIX}-report"
SCHEDULE="cron(0 4 1 * ? *)"

FUNCTION_ARN="$(aws lambda get-function --function-name "$FUNCTION_NAME" \
    --query Configuration.FunctionArn --output text 2>/dev/null || true)"
if [[ -z "$FUNCTION_ARN" ]]; then
  echo "ERROR: $FUNCTION_NAME not found. Run ./infrastructure/lambda/deploy_function.sh report first." >&2
  exit 1
fi

RULE_ARN="$(aws events put-rule --name "$RULE_NAME" \
    --schedule-expression "$SCHEDULE" --state ENABLED \
    --description "06:00 SAST on the 1st of each month: write last month's FinTrust report" \
    --tags Key=Project,Value=fintrust-capstone --query RuleArn --output text)"
echo "Rule    $RULE_NAME  $SCHEDULE"

FAILED="$(aws events put-targets --rule "$RULE_NAME" \
    --targets "Id=report-lambda,Arn=${FUNCTION_ARN}" --query FailedEntryCount --output text)"
if [[ "$FAILED" != "0" ]]; then
  echo "ERROR: EventBridge did not accept the target." >&2
  exit 1
fi
echo "Target  $FUNCTION_NAME"

# Allow this rule, and only this rule, to invoke the function.
if ! OUTPUT="$(aws lambda add-permission --function-name "$FUNCTION_NAME" \
      --statement-id "allow-eventbridge-monthly-report" --action lambda:InvokeFunction \
      --principal events.amazonaws.com --source-arn "$RULE_ARN" 2>&1)"; then
  if [[ "$OUTPUT" != *"ResourceConflictException"* ]]; then
    echo "$OUTPUT" >&2; exit 1
  fi
fi
echo "Permission granted to the rule only"

echo
aws events describe-rule --name "$RULE_NAME" \
    --query '{Rule:Name,Schedule:ScheduleExpression,State:State}' --output table
echo "Next run: 04:00 UTC (06:00 SAST) on the 1st of next month."
echo "To run it now for a chosen month:"
echo "  aws lambda invoke --function-name $FUNCTION_NAME --cli-binary-format raw-in-base64-out \\"
echo "      --payload '{\"month\": \"2026-09\"}' /dev/stdout"
