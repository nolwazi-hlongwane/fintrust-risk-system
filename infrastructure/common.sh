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
      -e "s|__ENV__|${FINTRUST_ENV}|g" "$1"
}

echo "Account ${ACCOUNT_ID} | region ${AWS_REGION} | caller ${CALLER_ARN}"
