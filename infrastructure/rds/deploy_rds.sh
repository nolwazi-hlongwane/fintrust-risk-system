#!/usr/bin/env bash
# Week 13 Day 2: the PostgreSQL ledger database.
#
# Creates one RDS PostgreSQL instance:
#   db.t3.micro, 20 GB            the Free Tier size named in the brief
#   private subnets, NOT publicly accessible (no public endpoint exists)
#   storage encrypted at rest
#   IAM database authentication enabled (Lambdas log in with signed tokens)
#   single-AZ                     Multi-AZ is the production design, outside Free Tier
#
# The master password is generated here, stored encrypted in SSM Parameter
# Store, and never printed. It is used only by you, through the tunnel, for
# schema changes. No Lambda can read it.
#
# Takes about 10 minutes. Safe to run more than once.
# Usage:  ./infrastructure/rds/deploy_rds.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

RDS_SG="$(sg_id rds)"
if [[ -z "$RDS_SG" ]]; then
  echo "ERROR: network not found. Run ./infrastructure/network/deploy_network.sh first." >&2
  exit 1
fi

STATUS="$(db_status)"
if [[ -z "$STATUS" ]]; then
  # Master password: create once, keep encrypted in SSM.
  if ! aws ssm get-parameter --name "$DB_MASTER_PARAM" >/dev/null 2>&1; then
    # Letters and digits only: RDS does not allow / @ " or spaces in passwords.
    PASSWORD="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32)"
    aws ssm put-parameter --name "$DB_MASTER_PARAM" --type SecureString --value "$PASSWORD" \
        --description "FinTrust RDS master password (human use via tunnel only)" >/dev/null
    echo "Generated the master password and stored it encrypted at $DB_MASTER_PARAM"
  fi
  PASSWORD="$(aws ssm get-parameter --name "$DB_MASTER_PARAM" --with-decryption \
      --query Parameter.Value --output text)"

  # Newest PostgreSQL 16 minor version available in this region
  # (or set PG_ENGINE_VERSION=16.x to choose one yourself).
  ENGINE_VERSION="${PG_ENGINE_VERSION:-}"
  if [[ -z "$ENGINE_VERSION" ]]; then
    ENGINE_VERSION="$(aws rds describe-db-engine-versions --engine postgres \
        --query "DBEngineVersions[?starts_with(EngineVersion, '16.')].EngineVersion | [-1]" --output text)"
  fi

  echo "Creating ${DB_INSTANCE} (PostgreSQL ${ENGINE_VERSION}, db.t3.micro)..."
  aws rds create-db-instance \
      --db-instance-identifier "$DB_INSTANCE" \
      --db-name "$DB_NAME" \
      --engine postgres --engine-version "$ENGINE_VERSION" \
      --db-instance-class db.t3.micro \
      --allocated-storage 20 --storage-type gp3 --storage-encrypted \
      --master-username "$DB_MASTER_USER" --master-user-password "$PASSWORD" \
      --db-subnet-group-name "${PREFIX}-db-subnets" \
      --vpc-security-group-ids "$RDS_SG" \
      --no-publicly-accessible \
      --no-multi-az \
      --enable-iam-database-authentication \
      --backup-retention-period 1 \
      --copy-tags-to-snapshot \
      --tags Key=Project,Value=fintrust-capstone >/dev/null
  unset PASSWORD
else
  echo "Database ${DB_INSTANCE} exists (status: ${STATUS})"
fi

echo "Waiting until the database is available (about 10 minutes on first create)..."
aws rds wait db-instance-available --db-instance-identifier "$DB_INSTANCE"

DB_HOST="$(aws rds describe-db-instances --db-instance-identifier "$DB_INSTANCE" \
    --query 'DBInstances[0].Endpoint.Address' --output text)"
PUBLIC="$(aws rds describe-db-instances --db-instance-identifier "$DB_INSTANCE" \
    --query 'DBInstances[0].PubliclyAccessible' --output text)"

echo "Saving connection settings:"
put_param db_host "$DB_HOST"
put_param db_port "5432"
put_param db_name "$DB_NAME"

echo
echo "Database ready."
echo "  Endpoint             $DB_HOST"
echo "  Publicly accessible  $PUBLIC   (must be False)"
echo
echo "Next: ./infrastructure/rds/deploy_tunnel.sh"
