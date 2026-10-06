#!/usr/bin/env bash
# Start the database (and the tunnel instance) before working or demonstrating.
# Takes about 5 minutes.
#
# Usage:  ./scripts/rds_start.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../infrastructure/common.sh"

STATUS="$(db_status)"
if [[ "$STATUS" == "stopped" ]]; then
  aws rds start-db-instance --db-instance-identifier "$DB_INSTANCE" >/dev/null
  echo "Starting database ${DB_INSTANCE}..."
elif [[ -z "$STATUS" ]]; then
  echo "ERROR: database ${DB_INSTANCE} not found" >&2; exit 1
fi

INSTANCE_ID="$(tunnel_instance_id)"
if [[ -n "$INSTANCE_ID" ]]; then
  aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
fi

aws rds wait db-instance-available --db-instance-identifier "$DB_INSTANCE"
echo "Database ${DB_INSTANCE} is available"
