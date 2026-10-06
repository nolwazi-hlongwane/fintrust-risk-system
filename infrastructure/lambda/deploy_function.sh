#!/usr/bin/env bash
# Package and deploy one Lambda function, then wire up what triggers it.
#
#   ./infrastructure/lambda/deploy_function.sh ingest
#
# What it does:
#   1. Builds a zip of the fintrust/ package in build/ (ignored by git).
#   2. Creates the function, or updates its code and settings if it exists.
#   3. Applies the function's own wiring. For ingest:
#        - failed invocations go to the ingest dead-letter queue (after 2 retries)
#        - the raw bucket may invoke the function (resource-based permission)
#        - the raw bucket sends an event for every new .csv object
#
# Safe to run more than once: run it again after any code change.

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
source "$HERE/../common.sh"

FN="${1:-}"
case "$FN" in
  ingest)
    HANDLER="fintrust.ingest.handler"
    TIMEOUT=120          # seconds; a 5 000-row file takes well under this
    MEMORY=256           # MB
    IN_VPC="no"          # needs SQS (no free VPC endpoint) and never touches RDS
    DESCRIPTION="FinTrust ETL: validate uploaded CSV rows, quarantine bad ones, queue good ones"
    ;;
  *)
    echo "Usage: $0 <function>" >&2
    echo "Functions available so far: ingest" >&2
    exit 1
    ;;
esac

FUNCTION_NAME="${PREFIX}-${FN}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${PREFIX}-${FN}-role"

# ---------- 1. build ----------
BUILD="$ROOT/build/$FN"
ZIP="$ROOT/build/$FN.zip"
rm -rf "$BUILD" "$ZIP"
mkdir -p "$BUILD"
cp -R "$ROOT/fintrust" "$BUILD/fintrust"
find "$BUILD" -name "__pycache__" -type d -prune -exec rm -rf {} +
# boto3 is already provided by the Lambda Python runtime, so it is not packaged.
(cd "$BUILD" && zip -qr "$ZIP" .)
echo "Built $(basename "$ZIP") ($(du -h "$ZIP" | cut -f1 | tr -d ' '))"

# ---------- 2. create or update ----------
ENVIRONMENT="Variables={FINTRUST_ENV=${FINTRUST_ENV}}"

if aws lambda get-function --function-name "$FUNCTION_NAME" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNCTION_NAME" --zip-file "fileb://$ZIP" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME"
  aws lambda update-function-configuration --function-name "$FUNCTION_NAME" \
      --handler "$HANDLER" --runtime python3.13 --role "$ROLE_ARN" \
      --timeout "$TIMEOUT" --memory-size "$MEMORY" \
      --environment "$ENVIRONMENT" --tracing-config Mode=Active >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME"
  echo "Updated function $FUNCTION_NAME"
else
  aws lambda create-function --function-name "$FUNCTION_NAME" \
      --description "$DESCRIPTION" \
      --runtime python3.13 --architectures x86_64 \
      --handler "$HANDLER" --role "$ROLE_ARN" \
      --zip-file "fileb://$ZIP" \
      --timeout "$TIMEOUT" --memory-size "$MEMORY" \
      --environment "$ENVIRONMENT" --tracing-config Mode=Active \
      --tags Project=fintrust-capstone >/dev/null
  aws lambda wait function-active-v2 --function-name "$FUNCTION_NAME"
  echo "Created function $FUNCTION_NAME"
fi
FUNCTION_ARN="$(aws lambda get-function --function-name "$FUNCTION_NAME" \
    --query Configuration.FunctionArn --output text)"

# ---------- 3. wiring ----------
if [[ "$FN" == "ingest" ]]; then
  RAW_BUCKET="$(bucket_name raw)"

  # S3 invokes this function asynchronously. If the function fails, Lambda
  # retries twice; if it still fails, the event goes to the dead-letter queue.
  aws lambda update-function-configuration --function-name "$FUNCTION_NAME" \
      --dead-letter-config "TargetArn=$(queue_arn "$INGEST_DLQ_NAME")" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME"
  aws lambda put-function-event-invoke-config --function-name "$FUNCTION_NAME" \
      --maximum-retry-attempts 2 --maximum-event-age-in-seconds 3600 >/dev/null
  echo "Dead-letter queue set to $INGEST_DLQ_NAME (after 2 retries)"

  # Allow S3, and only this bucket in this account, to invoke the function.
  if ! OUTPUT="$(aws lambda add-permission --function-name "$FUNCTION_NAME" \
        --statement-id "allow-s3-raw-bucket" --action lambda:InvokeFunction \
        --principal s3.amazonaws.com \
        --source-arn "arn:aws:s3:::${RAW_BUCKET}" --source-account "$ACCOUNT_ID" 2>&1)"; then
    if [[ "$OUTPUT" != *"ResourceConflictException"* ]]; then
      echo "$OUTPUT" >&2; exit 1
    fi
  fi

  # Tell the bucket to send an event for every new object whose name ends in .csv.
  aws s3api put-bucket-notification-configuration --bucket "$RAW_BUCKET" \
      --notification-configuration "{
        \"LambdaFunctionConfigurations\": [{
          \"Id\": \"csv-uploaded-run-ingest\",
          \"LambdaFunctionArn\": \"${FUNCTION_ARN}\",
          \"Events\": [\"s3:ObjectCreated:*\"],
          \"Filter\": {\"Key\": {\"FilterRules\": [{\"Name\": \"suffix\", \"Value\": \".csv\"}]}}
        }]}"
  echo "S3 trigger wired: ${RAW_BUCKET} (*.csv) -> ${FUNCTION_NAME}"
fi

echo
aws lambda get-function-configuration --function-name "$FUNCTION_NAME" \
    --query '{Function:FunctionName,Runtime:Runtime,Handler:Handler,Timeout:Timeout,Memory:MemorySize,State:State,DeadLetterQueue:DeadLetterConfig.TargetArn,Tracing:TracingConfig.Mode}' \
    --output table
