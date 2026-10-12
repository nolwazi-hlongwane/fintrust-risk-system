#!/usr/bin/env bash
# Package and deploy one Lambda function, then wire up what triggers it.
#
#   ./infrastructure/lambda/deploy_function.sh ingest
#   ./infrastructure/lambda/deploy_function.sh score
#   ./infrastructure/lambda/deploy_function.sh query
#   ./infrastructure/lambda/deploy_function.sh alert
#
# What it does:
#   1. Builds a zip of the fintrust/ package in build/ (ignored by git). For
#      functions that use the database, the PostgreSQL driver (psycopg2) built
#      for Lambda's Linux is added to the zip.
#   2. Creates the function, or updates its code and settings if it exists.
#      Functions that use the database are placed in the VPC's private subnets.
#   3. Applies the function's own wiring:
#        ingest  failed invocations go to the ingest dead-letter queue (after 2
#                retries); the raw bucket invokes it for every new .csv object
#        score   the transactions FIFO queue is connected as its event source
#        query   nothing here; API Gateway is connected by deploy_api.sh
#        alert   the events table's DynamoDB stream is connected, filtered so the
#                function is only invoked for HIGH risk scores
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
  score)
    HANDLER="fintrust.score.handler"
    TIMEOUT=30           # must stay below the queue's 60 second visibility timeout
    MEMORY=256
    IN_VPC="yes"         # needs the database
    DESCRIPTION="FinTrust scoring: apply risk rules, write the ledger (RDS) and the score (DynamoDB)"
    ;;
  query)
    HANDLER="fintrust.query.handler"
    TIMEOUT=15           # API Gateway gives up after 29 seconds
    MEMORY=256
    IN_VPC="yes"         # needs the database
    DESCRIPTION="FinTrust REST API: transactions, journey and risk insights"
    ;;
  alert)
    HANDLER="fintrust.alert.handler"
    TIMEOUT=30
    MEMORY=256
    IN_VPC="no"          # needs SES (no free VPC endpoint) and never touches RDS
    DESCRIPTION="FinTrust alerts: email compliance when a transaction is scored HIGH"
    ;;
  report)
    HANDLER="fintrust.report.handler"
    TIMEOUT=60
    MEMORY=256
    IN_VPC="yes"         # needs the database; reaches S3 through the gateway endpoint
    DESCRIPTION="FinTrust reporting: monthly CSV and JSON report to the reports bucket"
    ;;
  *)
    echo "Usage: $0 <function>" >&2
    echo "Functions: ingest, score, query, alert, report" >&2
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
if [[ "$FN" == "report" || "$FN" == "query" ]]; then
  # The report, and the API's running-balance chart, run the project's own SQL
  # files, so they travel with the code (one source of truth for the analytics).
  mkdir -p "$BUILD/fintrust/sql"
  cp "$ROOT/sql/monthly_summary.sql" "$ROOT/sql/risk_report.sql" "$BUILD/fintrust/sql/"
fi
# boto3 is already provided by the Lambda Python runtime, so it is not packaged.
if [[ "$IN_VPC" == "yes" ]]; then
  # psycopg2 contains compiled code, so the copy installed on a Mac will not run
  # on Lambda. This downloads the ready-built Linux version instead, pinned to
  # the same version as requirements.txt.
  PSYCOPG_VERSION="$(grep -E '^psycopg2-binary==' "$ROOT/requirements.txt" | cut -d= -f3)"
  python3 -m pip install --quiet --disable-pip-version-check \
      --platform manylinux2014_x86_64 --implementation cp --python-version 3.13 \
      --only-binary=:all: --no-deps --target "$BUILD" "psycopg2-binary==${PSYCOPG_VERSION}"
  rm -rf "$BUILD"/*.dist-info
fi
(cd "$BUILD" && zip -qr "$ZIP" .)
echo "Built $(basename "$ZIP") ($(du -h "$ZIP" | cut -f1 | tr -d ' '))"

# ---------- 2. create or update ----------
python3 "$HERE/build_env.py" "$FN" > "$ROOT/build/$FN-env.json"
ENVIRONMENT="file://$ROOT/build/$FN-env.json"

VPC_ARGS=()
if [[ "$IN_VPC" == "yes" ]]; then
  SUBNET_A="$(subnet_id a)"; SUBNET_B="$(subnet_id b)"; LAMBDA_SG="$(sg_id lambda)"
  if [[ -z "$SUBNET_A" || -z "$SUBNET_B" || -z "$LAMBDA_SG" ]]; then
    echo "ERROR: network not found. Run ./infrastructure/network/deploy_network.sh first." >&2
    exit 1
  fi
  # Two subnets in two Availability Zones: if one zone fails, Lambda runs in the other.
  VPC_ARGS=(--vpc-config "SubnetIds=${SUBNET_A},${SUBNET_B},SecurityGroupIds=${LAMBDA_SG}")
fi

if aws lambda get-function --function-name "$FUNCTION_NAME" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNCTION_NAME" --zip-file "fileb://$ZIP" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME"
  aws lambda update-function-configuration --function-name "$FUNCTION_NAME" \
      --handler "$HANDLER" --runtime python3.13 --role "$ROLE_ARN" \
      --timeout "$TIMEOUT" --memory-size "$MEMORY" \
      --environment "$ENVIRONMENT" --tracing-config Mode=Active ${VPC_ARGS[@]+"${VPC_ARGS[@]}"} >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME"
  echo "Updated function $FUNCTION_NAME"
else
  aws lambda create-function --function-name "$FUNCTION_NAME" \
      --description "$DESCRIPTION" \
      --runtime python3.13 --architectures x86_64 \
      --handler "$HANDLER" --role "$ROLE_ARN" \
      --zip-file "fileb://$ZIP" \
      --timeout "$TIMEOUT" --memory-size "$MEMORY" \
      --environment "$ENVIRONMENT" --tracing-config Mode=Active ${VPC_ARGS[@]+"${VPC_ARGS[@]}"} \
      --tags Project=fintrust-capstone >/dev/null
  if [[ "$IN_VPC" == "yes" ]]; then
    echo "Attaching the function to the VPC (this can take a minute or two)..."
  fi
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

if [[ "$FN" == "score" ]]; then
  QUEUE_ARN="$(queue_arn "$QUEUE_NAME")"
  MAPPING="$(aws lambda list-event-source-mappings --function-name "$FUNCTION_NAME" \
      --event-source-arn "$QUEUE_ARN" --query 'EventSourceMappings[0].UUID' --output text | none_to_empty)"
  if [[ -z "$MAPPING" ]]; then
    # The Lambda service polls the queue and invokes the function with up to 10
    # messages. ReportBatchItemFailures lets the function say exactly which
    # messages failed, so only those are retried.
    aws lambda create-event-source-mapping --function-name "$FUNCTION_NAME" \
        --event-source-arn "$QUEUE_ARN" --batch-size 10 \
        --function-response-types ReportBatchItemFailures >/dev/null
    echo "Queue connected: ${QUEUE_NAME} -> ${FUNCTION_NAME}"
  else
    aws lambda update-event-source-mapping --uuid "$MAPPING" --batch-size 10 \
        --function-response-types ReportBatchItemFailures >/dev/null
    echo "Queue already connected: ${QUEUE_NAME} -> ${FUNCTION_NAME}"
  fi
fi

if [[ "$FN" == "alert" ]]; then
  STREAM_ARN="$(aws dynamodb describe-table --table-name "$TABLE_NAME" \
      --query Table.LatestStreamArn --output text | none_to_empty)"
  if [[ -z "$STREAM_ARN" ]]; then
    echo "ERROR: the events table has no stream. Run ./infrastructure/dynamodb/deploy_table.sh first." >&2
    exit 1
  fi
  # The filter is applied by the Lambda service BEFORE invoking the function:
  # only new or changed SCORE items with risk_level HIGH get through. LOW and
  # MEDIUM scores and journey events never start the function, which keeps
  # invocations (and cost) to a minimum.
  cat > "$ROOT/build/alert-filter.json" <<'JSON'
{
  "Filters": [
    {
      "Pattern": "{\"eventName\": [\"INSERT\", \"MODIFY\"], \"dynamodb\": {\"NewImage\": {\"record_type\": {\"S\": [\"SCORE\"]}, \"risk_level\": {\"S\": [\"HIGH\"]}}}}"
    }
  ]
}
JSON
  # If sending still fails after 3 retries, the record's details go to the alert
  # dead-letter queue instead of blocking the stream. BisectBatchOnFunctionError
  # splits a failing batch in two so one bad record cannot hold back the others.
  cat > "$ROOT/build/alert-destination.json" <<JSON
{"OnFailure": {"Destination": "$(queue_arn "$ALERT_DLQ_NAME")"}}
JSON
  MAPPING="$(aws lambda list-event-source-mappings --function-name "$FUNCTION_NAME" \
      --event-source-arn "$STREAM_ARN" --query 'EventSourceMappings[0].UUID' --output text | none_to_empty)"
  if [[ -z "$MAPPING" ]]; then
    aws lambda create-event-source-mapping --function-name "$FUNCTION_NAME" \
        --event-source-arn "$STREAM_ARN" --starting-position LATEST --batch-size 10 \
        --maximum-retry-attempts 3 --maximum-record-age-in-seconds 3600 --bisect-batch-on-function-error \
        --filter-criteria "file://$ROOT/build/alert-filter.json" \
        --destination-config "file://$ROOT/build/alert-destination.json" >/dev/null
    echo "Stream connected: ${TABLE_NAME} (HIGH scores only) -> ${FUNCTION_NAME}"
  else
    aws lambda update-event-source-mapping --uuid "$MAPPING" --batch-size 10 \
        --maximum-retry-attempts 3 --maximum-record-age-in-seconds 3600 --bisect-batch-on-function-error \
        --filter-criteria "file://$ROOT/build/alert-filter.json" \
        --destination-config "file://$ROOT/build/alert-destination.json" >/dev/null
    echo "Stream already connected: ${TABLE_NAME} (HIGH scores only) -> ${FUNCTION_NAME}"
  fi
fi

echo
aws lambda get-function-configuration --function-name "$FUNCTION_NAME" \
    --query '{Function:FunctionName,Runtime:Runtime,Handler:Handler,Timeout:Timeout,Memory:MemorySize,State:State,VPC:VpcConfig.VpcId,DeadLetterQueue:DeadLetterConfig.TargetArn,Tracing:TracingConfig.Mode}' \
    --output table
