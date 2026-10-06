#!/usr/bin/env bash
# Shared settings for every infrastructure script.
# Usage inside another script:  source "$(dirname "$0")/../common.sh"

set -euo pipefail

export AWS_REGION="${AWS_REGION:-eu-north-1}"        # Europe (Stockholm)
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=""                                  # print output, do not open a pager
export PREFIX="fintrust-capstone"                    # every resource name starts with this
export FINTRUST_ENV="${FINTRUST_ENV:-dev}"
export FUNCTIONS="ingest score alert query report"   # the five Lambda functions

CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export CALLER_ARN ACCOUNT_ID

# Safety check: the root user must never be used for day-to-day work.
if [[ "$CALLER_ARN" == *":root" ]]; then
  echo "ERROR: you are signed in to the CLI as the ROOT user." >&2
  echo "Configure the AWS CLI with your IAM user instead, then run this again." >&2
  exit 1
fi

# Replace the placeholders in a policy template and print the result.
render() {
  sed -e "s|__ACCOUNT_ID__|${ACCOUNT_ID}|g" \
      -e "s|__REGION__|${AWS_REGION}|g" \
      -e "s|__PREFIX__|${PREFIX}|g" \
      -e "s|__ENV__|${FINTRUST_ENV}|g" \
      -e "s|__DBI_RESOURCE_ID__|${DBI_RESOURCE_ID:-__DBI_RESOURCE_ID__}|g" "$1"
}

# ---- names shared by several scripts ----------------------------------------
export DB_INSTANCE="${PREFIX}-db"
export DB_NAME="fintrust"
export DB_MASTER_USER="fintrust_admin"
export DB_MASTER_PARAM="/fintrust-admin/${FINTRUST_ENV}/db_master_password"   # human use only
export TABLE_NAME="${PREFIX}-events"
export TUNNEL_NAME="${PREFIX}-db-tunnel"
export TUNNEL_LOCAL_PORT="15432"

# ---- lookups: find a resource by its Name tag; print nothing if it is absent -
none_to_empty() { sed -e 's/^None$//'; }

vpc_id() {
  aws ec2 describe-vpcs --filters "Name=tag:Name,Values=${PREFIX}-vpc" \
      --query 'Vpcs[0].VpcId' --output text | none_to_empty
}

subnet_id() {   # subnet_id a   or   subnet_id b
  aws ec2 describe-subnets --filters "Name=tag:Name,Values=${PREFIX}-private-$1" \
      --query 'Subnets[0].SubnetId' --output text | none_to_empty
}

sg_id() {       # sg_id lambda | rds | tunnel | eice
  local vpc; vpc="$(vpc_id)"
  [[ -z "$vpc" ]] && return 0
  aws ec2 describe-security-groups \
      --filters "Name=vpc-id,Values=${vpc}" "Name=group-name,Values=${PREFIX}-$1-sg" \
      --query 'SecurityGroups[0].GroupId' --output text | none_to_empty
}

tunnel_instance_id() {
  aws ec2 describe-instances \
      --filters "Name=tag:Name,Values=${TUNNEL_NAME}" \
                "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query 'Reservations[0].Instances[0].InstanceId' --output text | none_to_empty
}

db_status() {
  aws rds describe-db-instances --db-instance-identifier "$DB_INSTANCE" \
      --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null || true
}

put_param() {   # put_param name value
  aws ssm put-parameter --name "/fintrust/${FINTRUST_ENV}/$1" --value "$2" \
      --type String --overwrite >/dev/null
  echo "  SSM /fintrust/${FINTRUST_ENV}/$1 = $2"
}

echo "Account ${ACCOUNT_ID} | region ${AWS_REGION} | caller ${CALLER_ARN}"
