#!/usr/bin/env bash
# Week 14 Day 5: stop the database and the tunnel instance every night, automatically.
#
# Why: the database is the only part of the system that costs money while idle.
# The brief says it must be "stopped outside lab hours", and AWS restarts a
# stopped database by itself after 7 days. Relying on memory for months until
# the presentation is a cost risk, so this removes the human step.
#
# Two EventBridge Scheduler schedules run at 20:00 South African time each day:
#     fintrust-capstone-stop-database   calls rds:StopDBInstance
#     fintrust-capstone-stop-tunnel     calls ec2:StopInstances
# Scheduler calls the AWS API directly ("universal target"); no Lambda is needed.
# Its role may stop this one database and this one instance, and nothing else.
#
# If the database is already stopped the call simply fails and is not retried.
# Starting stays manual and deliberate: ./scripts/rds_start.sh
#
# Cost: EventBridge Scheduler's free tier is 14 million invocations a month.
# Safe to run more than once.
# Usage:  ./infrastructure/scheduler/deploy_nightly_stop.sh
#         STOP_HOUR=22 ./infrastructure/scheduler/deploy_nightly_stop.sh    (a different hour)

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

STOP_HOUR="${STOP_HOUR:-20}"
ROLE_NAME="${PREFIX}-scheduler-role"
TUNNEL_INSTANCE_ID="$(tunnel_instance_id)"
if [[ -z "$TUNNEL_INSTANCE_ID" ]]; then
  echo "ERROR: tunnel instance not found. Run ./infrastructure/rds/deploy_tunnel.sh first." >&2
  exit 1
fi
if [[ -z "$(db_status)" ]]; then
  echo "ERROR: database ${DB_INSTANCE} not found." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
render "$HERE/trust-scheduler.json" > "$WORK/trust.json"
render "$HERE/scheduler-policy.json" | sed "s|__TUNNEL_INSTANCE_ID__|${TUNNEL_INSTANCE_ID}|g" > "$WORK/policy.json"

# ---------- role ----------
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$WORK/trust.json"
else
  aws iam create-role --role-name "$ROLE_NAME" \
      --description "Lets EventBridge Scheduler stop the FinTrust database and tunnel instance" \
      --assume-role-policy-document "file://$WORK/trust.json" \
      --tags Key=Project,Value=fintrust-capstone >/dev/null
  echo "Created role $ROLE_NAME"
  sleep 10        # a new role takes a few seconds to become usable
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "${PREFIX}-nightly-stop" \
    --policy-document "file://$WORK/policy.json"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# ---------- schedules ----------
schedule() {   # schedule <name> <target arn> <input json> <description>
  cat > "$WORK/target.json" <<JSON
{"Arn": "$2", "RoleArn": "${ROLE_ARN}", "Input": $(printf '%s' "$3" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'), "RetryPolicy": {"MaximumRetryAttempts": 0}}
JSON
  local action="create-schedule"
  if aws scheduler get-schedule --name "$1" >/dev/null 2>&1; then action="update-schedule"; fi
  aws scheduler "$action" --name "$1" --description "$4" \
      --schedule-expression "cron(0 ${STOP_HOUR} * * ? *)" \
      --schedule-expression-timezone "Africa/Johannesburg" \
      --flexible-time-window Mode=OFF --state ENABLED \
      --target "file://$WORK/target.json" >/dev/null
  echo "Schedule  $1  every day at ${STOP_HOUR}:00 South African time"
}
schedule "${PREFIX}-stop-database" "arn:aws:scheduler:::aws-sdk:rds:stopDBInstance" \
    "{\"DbInstanceIdentifier\": \"${DB_INSTANCE}\"}" "Stop the FinTrust database every night to protect credits"
schedule "${PREFIX}-stop-tunnel" "arn:aws:scheduler:::aws-sdk:ec2:stopInstances" \
    "{\"InstanceIds\": [\"${TUNNEL_INSTANCE_ID}\"]}" "Stop the FinTrust database tunnel instance every night"

echo
aws scheduler list-schedules --name-prefix "${PREFIX}-stop" \
    --query 'Schedules[].{Schedule:Name,State:State}' --output table
echo "Before working or demonstrating:  ./scripts/rds_start.sh"
