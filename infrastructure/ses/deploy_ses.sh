#!/usr/bin/env bash
# Week 14 Day 1: verify the email address used for high-risk alerts.
#
# Amazon SES starts every account in a "sandbox": it will only send FROM an
# address you have proved you own, and only TO addresses that are verified too.
# That is exactly right for this project (the sender and the compliance
# recipient are the same verified address), and it keeps usage inside the Free
# Tier. Moving out of the sandbox for production needs a request to AWS.
#
# This script asks SES to email a verification link to the address stored in
# SSM (/fintrust/dev/alert_sender and alert_recipient). The link must be clicked once
# before SES will send anything.
#
# Safe to run more than once: run it again to check the status.
# Usage:  ./infrastructure/ses/deploy_ses.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

status_of() {
  aws ses get-identity-verification-attributes --identities "$1" \
      --query "VerificationAttributes.\"$1\".VerificationStatus" --output text | none_to_empty
}

ALL_VERIFIED="yes"
for NAME in alert_sender alert_recipient; do
  ADDRESS="$(aws ssm get-parameter --name "/fintrust/${FINTRUST_ENV}/${NAME}" \
      --query Parameter.Value --output text)"
  STATUS="$(status_of "$ADDRESS")"
  if [[ "$STATUS" == "Success" ]]; then
    echo "Verified      ${NAME}: ${ADDRESS}"
  else
    if [[ -z "$STATUS" ]]; then
      aws ses verify-email-identity --email-address "$ADDRESS"
      echo "Email sent    ${NAME}: ${ADDRESS}"
    else
      echo "Waiting       ${NAME}: ${ADDRESS} (status: ${STATUS})"
    fi
    ALL_VERIFIED="no"
  fi
done

echo
if [[ "$ALL_VERIFIED" == "yes" ]]; then
  echo "SES is ready to send alerts."
else
  echo "ACTION NEEDED: open the email from Amazon Web Services with the subject"
  echo "\"Amazon Web Services - Email Address Verification Request in region Europe (Stockholm)\""
  echo "and click the link in it (check the spam folder too). Then run this script again."
  exit 2
fi
