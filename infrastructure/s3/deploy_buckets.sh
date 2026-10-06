#!/usr/bin/env bash
# Week 13 Day 3: the four data buckets.
#
#   raw         CSV files exactly as uploaded
#   processed   the validated, cleaned rows (ETL output)
#   quarantine  rows that failed validation, with the reason
#   reports     monthly reports
#
# Every bucket gets the same protections:
#   1. Block Public Access, all four settings on
#   2. Default encryption with the project KMS key (SSE-KMS). "Bucket key" is on,
#      which lets S3 reuse a data key instead of calling KMS for every object,
#      keeping KMS requests (and cost) low.
#   3. A bucket policy that refuses any request not using TLS
#   4. Versioning, so an overwritten or deleted object can be recovered
#   5. A lifecycle rule (see lifecycle-<bucket>.json):
#        raw, processed, reports: Standard-IA after 30 days, Glacier after
#        3 years, deleted after 7 years (the regulatory retention period)
#        quarantine: deleted after 90 days
#
# Safe to run more than once.
# Usage:  ./infrastructure/s3/deploy_buckets.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

KMS_KEY_ARN="$(kms_key_arn)"
if [[ -z "$KMS_KEY_ARN" ]]; then
  echo "ERROR: KMS key not found. Run ./infrastructure/kms/deploy_key.sh first." >&2
  exit 1
fi
export KMS_KEY_ARN

RENDERED="$HERE/rendered"   # ignored by git: contains your account id
mkdir -p "$RENDERED"

for NAME in $BUCKETS; do
  BUCKET="$(bucket_name "$NAME")"

  if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    echo "Bucket exists   $BUCKET"
  else
    aws s3api create-bucket --bucket "$BUCKET" \
        --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    echo "Created bucket  $BUCKET"
  fi

  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

  aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration "{
      \"Rules\": [{
        \"ApplyServerSideEncryptionByDefault\": {\"SSEAlgorithm\": \"aws:kms\", \"KMSMasterKeyID\": \"${KMS_KEY_ARN}\"},
        \"BucketKeyEnabled\": true
      }]}"

  aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled

  render "$HERE/bucket-policy.json" | sed "s|__BUCKET__|${BUCKET}|g" > "$RENDERED/policy-${NAME}.json"
  aws s3api put-bucket-policy --bucket "$BUCKET" --policy "file://$RENDERED/policy-${NAME}.json"

  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
      --lifecycle-configuration "file://$HERE/lifecycle-${NAME}.json"

  aws s3api put-bucket-tagging --bucket "$BUCKET" \
      --tagging "TagSet=[{Key=Project,Value=fintrust-capstone},{Key=DataTier,Value=${NAME}}]"
done

echo "Saving settings:"
for NAME in $BUCKETS; do
  put_param "${NAME}_bucket" "$(bucket_name "$NAME")"
done
