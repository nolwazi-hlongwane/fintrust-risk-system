#!/usr/bin/env bash
# Attach each Lambda role's DATA permissions (database, DynamoDB, and later S3,
# SQS, KMS and SES). Run again whenever a policies/<function>-data.json changes.
#
# The base permissions (logs, tracing, network) come from deploy_roles.sh.
# Data permissions are separate so a role only ever names resources that exist.
#
# Usage:  ./infrastructure/iam/apply_data_policies.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

POLICIES="$HERE/policies"
RENDERED="$HERE/rendered"
mkdir -p "$RENDERED"

# IAM database authentication is granted per database user on ONE database,
# identified by its resource id (looks like db-ABCDEFGHIJKL), not its name.
DBI_RESOURCE_ID="$(aws rds describe-db-instances --db-instance-identifier "$DB_INSTANCE" \
    --query 'DBInstances[0].DbiResourceId' --output text 2>/dev/null || true)"
export DBI_RESOURCE_ID

# The project encryption key, by its full ARN (an alias cannot be used in an IAM policy).
KMS_KEY_ARN="$(kms_key_arn)"
export KMS_KEY_ARN

for FN in $FUNCTIONS; do
  TEMPLATE="$POLICIES/${FN}-data.json"
  [[ -f "$TEMPLATE" ]] || continue
  render "$TEMPLATE" > "$RENDERED/${FN}-data.json"
  if grep -q "__[A-Z_]*__" "$RENDERED/${FN}-data.json"; then
    echo "ERROR: ${FN}-data.json still has an unfilled value:" >&2
    grep -o "__[A-Z_]*__" "$RENDERED/${FN}-data.json" | sort -u >&2
    echo "The database (deploy_rds.sh) and the KMS key (deploy_key.sh) must exist first." >&2
    exit 1
  fi
  aws iam put-role-policy --role-name "${PREFIX}-${FN}-role" \
      --policy-name "${PREFIX}-${FN}-data" \
      --policy-document "file://$RENDERED/${FN}-data.json"
  echo "Applied data policy to ${PREFIX}-${FN}-role"
done
