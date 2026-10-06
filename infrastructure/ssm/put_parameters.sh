#!/usr/bin/env bash
# Week 13 Day 1: store application configuration in SSM Parameter Store.
#
# Parameter Store is the single source of truth for settings. Values live under
# /fintrust/<env>/ and are read by fintrust/config.py.
#
# Usage:
#   Store the Day 1 settings (your email is typed here, never committed to git):
#       ALERT_EMAIL=you@example.com ./infrastructure/ssm/put_parameters.sh
#
#   Store or change one setting later (used when new resources are created):
#       ./infrastructure/ssm/put_parameters.sh raw_bucket fintrust-capstone-raw-123456789012

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

put() {
  aws ssm put-parameter --name "/fintrust/${FINTRUST_ENV}/$1" --value "$2" \
      --type String --overwrite >/dev/null
  echo "  /fintrust/${FINTRUST_ENV}/$1"
}

if [[ $# -eq 2 ]]; then
  put "$1" "$2"
  exit 0
fi

if [[ -z "${ALERT_EMAIL:-}" ]]; then
  echo "ERROR: set ALERT_EMAIL, e.g.  ALERT_EMAIL=you@example.com $0" >&2
  exit 1
fi

echo "Writing parameters:"
# Who sends and receives high-risk alerts (both must be verified in SES sandbox)
put alert_sender    "$ALERT_EMAIL"
put alert_recipient "$ALERT_EMAIL"

# Risk scoring rules: change a value here to change behaviour without a deploy
put high_amount_threshold   "50000"
put high_amount_points      "50"
put velocity_limit          "10"
put velocity_window_minutes "60"
put velocity_points         "30"
put home_country            "ZA"
put cross_border_points     "20"
put medium_risk_score       "40"
put high_risk_score         "70"

put metrics_namespace "FinTrust/ETL"

echo
aws ssm get-parameters-by-path --path "/fintrust/${FINTRUST_ENV}/" \
    --query 'Parameters[].[Name,Value]' --output table
