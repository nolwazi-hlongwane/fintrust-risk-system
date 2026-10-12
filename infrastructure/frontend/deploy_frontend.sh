#!/usr/bin/env bash
# Week 14 Day 4: host the web dashboard.
#
#   Browser --HTTPS--> CloudFront --signed request--> private S3 bucket
#
#   1. A PRIVATE S3 bucket holds the dashboard files. All public access is
#      blocked. It is not an "S3 website": nobody can fetch from it directly.
#   2. A CloudFront distribution serves the files over HTTPS. Origin Access
#      Control (OAC) makes CloudFront sign its requests to S3, and the bucket
#      policy accepts requests from this one distribution only.
#   3. A response headers policy adds security headers to every file, including a
#      Content-Security-Policy: the page may run only its own script and may
#      call only this project's API.
#   4. The API is then told to accept browser requests from the dashboard's
#      address and nowhere else (CORS), and the API Lambda and API are redeployed
#      to pick that up.
#
# The dashboard files hold no secret. The API key is typed in by the user.
# The bucket uses S3's own encryption (SSE-S3), not the project KMS key: these
# are public web pages, and CloudFront would otherwise need rights on the key.
#
# Free Tier: CloudFront includes 1 TB of data and 10 million requests a month.
# Safe to run more than once: run it again after changing a file in frontend/.
# The first run takes 5 to 15 minutes while CloudFront rolls out worldwide.
#
# Usage:  ./infrastructure/frontend/deploy_frontend.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
source "$HERE/../common.sh"

BUCKET="${PREFIX}-dashboard-${ACCOUNT_ID}"
NAME="${PREFIX}-dashboard"
HEADERS_NAME="${PREFIX}-security-headers"
WORK="$ROOT/build/frontend"
rm -rf "$WORK" && mkdir -p "$WORK"

API_URL="$(aws ssm get-parameter --name "/fintrust/${FINTRUST_ENV}/api_url" \
    --query Parameter.Value --output text 2>/dev/null || true)"
if [[ -z "$API_URL" ]]; then
  echo "ERROR: the API address is not in SSM. Run ./infrastructure/apigateway/deploy_api.sh first." >&2
  exit 1
fi
API_ORIGIN="$(echo "$API_URL" | cut -d/ -f1-3)"       # https://<id>.execute-api.<region>.amazonaws.com

# ---------- 1. private bucket ----------
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  aws s3api create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  echo "Created bucket $BUCKET"
else
  echo "Bucket $BUCKET exists"
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=Project,Value=fintrust-capstone}]'

# ---------- 2. origin access control: CloudFront signs its requests to S3 ----------
OAC_ID="$(aws cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='${NAME}'].Id | [0]" --output text | none_to_empty)"
if [[ -z "$OAC_ID" ]]; then
  OAC_ID="$(aws cloudfront create-origin-access-control --origin-access-control-config \
      "Name=${NAME},Description=FinTrust-dashboard,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
      --query OriginAccessControl.Id --output text)"
  echo "Created origin access control"
fi

# ---------- 3. security headers ----------
render "$HERE/security-headers.json" | sed "s|__API_ORIGIN__|${API_ORIGIN}|g" > "$WORK/security-headers.json"
HEADERS_ID="$(aws cloudfront list-response-headers-policies --type custom \
    --query "ResponseHeadersPolicyList.Items[?ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name=='${HEADERS_NAME}'].ResponseHeadersPolicy.Id | [0]" \
    --output text | none_to_empty)"
if [[ -z "$HEADERS_ID" ]]; then
  HEADERS_ID="$(aws cloudfront create-response-headers-policy \
      --response-headers-policy-config "file://$WORK/security-headers.json" \
      --query ResponseHeadersPolicy.Id --output text)"
  echo "Created security headers policy"
else
  ETAG="$(aws cloudfront get-response-headers-policy --id "$HEADERS_ID" --query ETag --output text)"
  aws cloudfront update-response-headers-policy --id "$HEADERS_ID" --if-match "$ETAG" \
      --response-headers-policy-config "file://$WORK/security-headers.json" >/dev/null
  echo "Updated security headers policy"
fi

# ---------- 4. distribution ----------
DISTRIBUTION_ID="$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='${NAME}'].Id | [0]" --output text | none_to_empty)"
if [[ -z "$DISTRIBUTION_ID" ]]; then
  render "$HERE/distribution.json" \
      | sed -e "s|__BUCKET__|${BUCKET}|g" -e "s|__OAC_ID__|${OAC_ID}|g" \
            -e "s|__HEADERS_POLICY_ID__|${HEADERS_ID}|g" > "$WORK/distribution.json"
  DISTRIBUTION_ID="$(aws cloudfront create-distribution \
      --distribution-config "file://$WORK/distribution.json" --query Distribution.Id --output text)"
  echo "Created CloudFront distribution $DISTRIBUTION_ID"
else
  echo "CloudFront distribution $DISTRIBUTION_ID exists"
fi
DOMAIN="$(aws cloudfront get-distribution --id "$DISTRIBUTION_ID" --query Distribution.DomainName --output text)"
DASHBOARD_URL="https://${DOMAIN}"

# ---------- 5. bucket policy: this distribution only, and only over TLS ----------
render "$HERE/bucket-policy.json" \
    | sed -e "s|__BUCKET__|${BUCKET}|g" -e "s|__DISTRIBUTION_ID__|${DISTRIBUTION_ID}|g" > "$WORK/bucket-policy.json"
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "file://$WORK/bucket-policy.json"
echo "Bucket policy: readable by distribution $DISTRIBUTION_ID only"

# ---------- 6. upload the files ----------
# config.js is written here with the real API address, so nothing is hard-coded
# in the repository. It contains no secret.
cp "$ROOT/frontend/index.html" "$ROOT/frontend/styles.css" "$ROOT/frontend/app.js" "$ROOT/frontend/favicon.svg" "$WORK/"
cat > "$WORK/config.js" <<CONFIG
// Written by infrastructure/frontend/deploy_frontend.sh. Holds no secret.
window.FINTRUST_CONFIG = { apiUrl: "${API_URL}" };
CONFIG
upload() {   # upload <file> <content type>
  aws s3 cp "$WORK/$1" "s3://${BUCKET}/$1" --content-type "$2" \
      --cache-control "no-cache" --only-show-errors
  echo "  uploaded $1"
}
upload index.html "text/html; charset=utf-8"
upload styles.css "text/css; charset=utf-8"
upload app.js     "application/javascript; charset=utf-8"
upload config.js  "application/javascript; charset=utf-8"
upload favicon.svg "image/svg+xml"

# Tell CloudFront's edge locations to drop their cached copies.
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/*" >/dev/null
echo "Cache invalidated"

# ---------- 7. let the dashboard, and only the dashboard, call the API from a browser ----------
echo "Saving settings:"
put_param cors_allowed_origin "$DASHBOARD_URL"
put_param dashboard_url "$DASHBOARD_URL"
"$HERE/../lambda/deploy_function.sh" query >/dev/null
echo "API Lambda redeployed with the new allowed origin"
"$HERE/../apigateway/deploy_api.sh" >/dev/null
echo "API redeployed"

STATUS="$(aws cloudfront get-distribution --id "$DISTRIBUTION_ID" --query Distribution.Status --output text)"
if [[ "$STATUS" != "Deployed" ]]; then
  echo
  echo "Waiting for CloudFront to finish rolling out (5 to 15 minutes the first time)..."
  aws cloudfront wait distribution-deployed --id "$DISTRIBUTION_ID"
fi

echo
echo "Dashboard ready:  $DASHBOARD_URL"
echo "Sign in with the API key. To copy it to the clipboard without showing it:"
echo "    source ./scripts/api_env.sh && printf '%s' \"\$API_KEY\" | pbcopy"
