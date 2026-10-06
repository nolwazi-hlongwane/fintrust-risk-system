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
# Key policy: the default one, which hands control to IAM in this account.
# Which role may do what with the key is then written in each role's own policy
# (infrastructure/iam/policies/*-data.json), next to its other permissions.
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

echo
echo "  Key ARN    $KEY_ARN"
echo "  Rotation   $(aws kms get-key-rotation-status --key-id "$KEY_ARN" --query KeyRotationEnabled --output text)"
