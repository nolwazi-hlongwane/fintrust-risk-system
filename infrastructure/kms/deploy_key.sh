#!/usr/bin/env bash
# Week 13 Day 3: the project's encryption key.
#
# Creates one customer-managed KMS key, alias/fintrust-capstone, used to encrypt
# the four S3 buckets and the SQS queues.
#
# Why a customer-managed key instead of the free AWS-managed one?
#   * WE decide who may use it. A role that can read a bucket but is not allowed
#     to use this key still cannot read the data: two locks instead of one.
#   * Every use of the key is recorded in CloudTrail.
#   * Automatic yearly rotation is switched on.
# Cost: about 1 USD per month, paid from credits.
#
# Key policy (key-policy.json), two statements:
#   1. The AWS default: control of the key is handed to IAM in this account.
#      Which role may do what with the key is then written in each role's own
#      policy (infrastructure/iam/policies/*-data.json), next to its other
#      permissions. ("kms:*" here grants nothing by itself to any role; it only
#      says "IAM policies in this account decide".)
#   2. CloudWatch may use the key, so that an alarm can publish to the
#      encrypted alarm topic (Week 14 Day 2). An AWS service is not an IAM role,
#      so it can only be allowed here, in the key policy.
#
# Safe to run more than once.
# Usage:  ./infrastructure/kms/deploy_key.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

KEY_ARN="$(kms_key_arn)"
if [[ -z "$KEY_ARN" ]]; then
  KEY_ID="$(aws kms create-key \
      --description "FinTrust capstone: encrypts transaction files and queue messages" \
      --key-usage ENCRYPT_DECRYPT --key-spec SYMMETRIC_DEFAULT \
      --tags TagKey=Project,TagValue=fintrust-capstone \
      --query KeyMetadata.KeyId --output text)"
  aws kms create-alias --alias-name "$KEY_ALIAS" --target-key-id "$KEY_ID"
  KEY_ARN="$(kms_key_arn)"
  echo "Created key $KEY_ALIAS"
else
  echo "Key $KEY_ALIAS exists"
fi

aws kms enable-key-rotation --key-id "$KEY_ARN"

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
render "$HERE/key-policy.json" > "$POLICY_FILE"
aws kms put-key-policy --key-id "$KEY_ARN" --policy-name default --policy "file://$POLICY_FILE"
echo "Applied key policy"

echo
echo "  Key ARN    $KEY_ARN"
echo "  Rotation   $(aws kms get-key-rotation-status --key-id "$KEY_ARN" --query KeyRotationEnabled --output text)"
