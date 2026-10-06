#!/usr/bin/env bash
# Stop the database and the tunnel instance when you finish working, so they
# stop using credits. Data is kept. Run ./scripts/rds_start.sh to resume.
#
# Note: AWS automatically restarts a stopped database after 7 days.
#
# Usage:  ./scripts/rds_stop.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../infrastructure/common.sh"

STATUS="$(db_status)"
if [[ "$STATUS" == "available" ]]; then
  aws rds stop-db-instance --db-instance-identifier "$DB_INSTANCE" >/dev/null
  echo "Stopping database ${DB_INSTANCE}"
else
  echo "Database ${DB_INSTANCE} is '${STATUS:-not found}', nothing to stop"
fi

INSTANCE_ID="$(tunnel_instance_id)"
if [[ -n "$INSTANCE_ID" ]]; then
  aws ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null
  echo "Stopping tunnel instance ${INSTANCE_ID}"
fi
