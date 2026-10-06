#!/usr/bin/env bash
# Week 13 Day 1: create the five Lambda execution roles.
#
# What this creates:
#   1. One permission boundary policy  (the maximum any FinTrust role may ever have)
#   2. Five log groups with 30-day retention (so the roles never need CreateLogGroup)
#   3. Five roles, one per Lambda, each with the boundary and a "base" inline policy
#
# Data permissions (S3, SQS, DynamoDB, RDS, KMS, SES) are added on the day each
# resource is created, so a role never has access to something that does not exist.
#
# Safe to run more than once: existing resources are updated, not duplicated.
#
# Usage:  ./infrastructure/iam/deploy_roles.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

POLICIES="$HERE/policies"
RENDERED="$HERE/rendered"          # ignored by git: contains your account id
mkdir -p "$RENDERED"

BOUNDARY_NAME="${PREFIX}-permission-boundary"
BOUNDARY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${BOUNDARY_NAME}"

# ---------- 1. permission boundary ----------
render "$POLICIES/permission-boundary.json" > "$RENDERED/permission-boundary.json"

if aws iam get-policy --policy-arn "$BOUNDARY_ARN" >/dev/null 2>&1; then
  # IAM keeps at most 5 versions of a policy. Delete the oldest unused one first.
  SPARE="$(aws iam list-policy-versions --policy-arn "$BOUNDARY_ARN" \
      --query 'length(Versions[?IsDefaultVersion==`false`])' --output text)"
  if [[ "$SPARE" -ge 4 ]]; then
    OLDEST="$(aws iam list-policy-versions --policy-arn "$BOUNDARY_ARN" \
        --query 'sort_by(Versions[?IsDefaultVersion==`false`], &CreateDate)[0].VersionId' --output text)"
    aws iam delete-policy-version --policy-arn "$BOUNDARY_ARN" --version-id "$OLDEST"
  fi
  aws iam create-policy-version --policy-arn "$BOUNDARY_ARN" \
      --policy-document "file://$RENDERED/permission-boundary.json" --set-as-default >/dev/null
  echo "Updated permission boundary  $BOUNDARY_NAME"
else
  aws iam create-policy --policy-name "$BOUNDARY_NAME" \
      --description "Maximum permissions for any FinTrust capstone Lambda role" \
      --policy-document "file://$RENDERED/permission-boundary.json" \
      --tags Key=Project,Value=fintrust-capstone >/dev/null
  echo "Created permission boundary  $BOUNDARY_NAME"
fi

# ---------- 2 and 3. log groups and roles ----------
for FN in $FUNCTIONS; do
  ROLE_NAME="${PREFIX}-${FN}-role"
  LOG_GROUP="/aws/lambda/${PREFIX}-${FN}"

  if [[ -z "$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
        --query "logGroups[?logGroupName=='$LOG_GROUP'].logGroupName" --output text)" ]]; then
    aws logs create-log-group --log-group-name "$LOG_GROUP" \
        --tags Project=fintrust-capstone
  fi
  aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30

  if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
        --policy-document "file://$POLICIES/trust-lambda.json"
    aws iam put-role-permissions-boundary --role-name "$ROLE_NAME" \
        --permissions-boundary "$BOUNDARY_ARN"
    echo "Updated role  $ROLE_NAME"
  else
    aws iam create-role --role-name "$ROLE_NAME" \
        --description "Execution role for the FinTrust ${FN} Lambda" \
        --assume-role-policy-document "file://$POLICIES/trust-lambda.json" \
        --permissions-boundary "$BOUNDARY_ARN" \
        --tags Key=Project,Value=fintrust-capstone Key=Function,Value="$FN" >/dev/null
    echo "Created role  $ROLE_NAME"
  fi

  render "$POLICIES/${FN}-base.json" > "$RENDERED/${FN}-base.json"
  aws iam put-role-policy --role-name "$ROLE_NAME" \
      --policy-name "${PREFIX}-${FN}-base" \
      --policy-document "file://$RENDERED/${FN}-base.json"
done

echo
echo "Done. Roles and their permission boundary:"
for FN in $FUNCTIONS; do
  # get-role is used because list-roles does not return the boundary.
  aws iam get-role --role-name "${PREFIX}-${FN}-role" \
      --query 'Role.[RoleName, PermissionsBoundary.PermissionsBoundaryArn]' --output text
done
