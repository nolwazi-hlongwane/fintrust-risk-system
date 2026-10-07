#!/usr/bin/env bash
# Week 13 Day 5: the REST API in front of the query Lambda.
#
# Creates (or updates) from openapi.json:
#   REST API  fintrust-capstone-api, regional endpoint, stage "dev"
#   Every route requires an API key in the x-api-key header. API Gateway itself
#   rejects a request without a valid key (403) before the Lambda is ever called.
#   Usage plan: at most 10 requests per second (burst 20) and 5 000 per day.
#   One API key attached to that usage plan.
#
# REST API (not the cheaper "HTTP API") is used because only REST APIs support
# API keys and usage plans, which the brief requires.
#
# Safe to run more than once: run it again after changing openapi.json.
# Usage:  ./infrastructure/apigateway/deploy_api.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
source "$HERE/../common.sh"

API_NAME="${PREFIX}-api"
STAGE="${FINTRUST_ENV}"
FUNCTION_NAME="${PREFIX}-query"

LAMBDA_ARN="$(aws lambda get-function --function-name "$FUNCTION_NAME" \
    --query Configuration.FunctionArn --output text 2>/dev/null || true)"
if [[ -z "$LAMBDA_ARN" ]]; then
  echo "ERROR: ${FUNCTION_NAME} not found. Run ./infrastructure/lambda/deploy_function.sh query first." >&2
  exit 1
fi

mkdir -p "$ROOT/build"
DEFINITION="$ROOT/build/openapi.json"
render "$HERE/openapi.json" | sed "s|__LAMBDA_ARN__|${LAMBDA_ARN}|g" > "$DEFINITION"

# ---------- API ----------
API_ID="$(aws apigateway get-rest-apis --query "items[?name=='${API_NAME}'].id | [0]" --output text | none_to_empty)"
if [[ -z "$API_ID" ]]; then
  API_ID="$(aws apigateway import-rest-api --parameters endpointConfigurationTypes=REGIONAL \
      --body "fileb://$DEFINITION" --query id --output text)"
  echo "Created API $API_NAME ($API_ID)"
else
  aws apigateway put-rest-api --rest-api-id "$API_ID" --mode overwrite \
      --body "fileb://$DEFINITION" >/dev/null
  echo "Updated API $API_NAME ($API_ID)"
fi

# ---------- make sure every route (except the browser preflight) needs the key ----------
# The definition already says so; this sets it explicitly as well, so a mistake
# in openapi.json can never publish an open route.
ROUTES="$(aws apigateway get-resources --rest-api-id "$API_ID" --limit 500 \
    --query 'items[?resourceMethods].[id, join(`,`, keys(resourceMethods))]' --output text)"
while read -r RESOURCE_ID METHODS; do
  [[ -z "$RESOURCE_ID" ]] && continue
  for METHOD in ${METHODS//,/ }; do
    if [[ "$METHOD" != "OPTIONS" ]]; then
      aws apigateway update-method --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
          --http-method "$METHOD" --patch-operations op=replace,path=/apiKeyRequired,value=true >/dev/null
    fi
  done
done <<< "$ROUTES"
echo "API key required on every route"

# ---------- allow this API, and only this API, to invoke the Lambda ----------
if ! OUTPUT="$(aws lambda add-permission --function-name "$FUNCTION_NAME" \
      --statement-id "allow-api-gateway" --action lambda:InvokeFunction \
      --principal apigateway.amazonaws.com \
      --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*/*" 2>&1)"; then
  if [[ "$OUTPUT" != *"ResourceConflictException"* ]]; then
    echo "$OUTPUT" >&2; exit 1
  fi
fi

# ---------- publish to the stage ----------
# Changes to an API are not live until they are deployed to a stage.
aws apigateway create-deployment --rest-api-id "$API_ID" --stage-name "$STAGE" \
    --description "Deployed by deploy_api.sh" >/dev/null
echo "Deployed to stage $STAGE"

# X-Ray: trace each request from API Gateway into the Lambda (Week 14 Day 2).
aws apigateway update-stage --rest-api-id "$API_ID" --stage-name "$STAGE" \
    --patch-operations op=replace,path=/tracingEnabled,value=true >/dev/null
echo "X-Ray tracing enabled on stage $STAGE"

# ---------- usage plan and API key ----------
PLAN_NAME="${PREFIX}-usage-plan"
PLAN_ID="$(aws apigateway get-usage-plans --query "items[?name=='${PLAN_NAME}'].id | [0]" --output text | none_to_empty)"
if [[ -z "$PLAN_ID" ]]; then
  PLAN_ID="$(aws apigateway create-usage-plan --name "$PLAN_NAME" \
      --description "Compliance and fraud team access" \
      --throttle burstLimit=20,rateLimit=10 --quota limit=5000,period=DAY \
      --api-stages "apiId=${API_ID},stage=${STAGE}" --query id --output text)"
  echo "Created usage plan $PLAN_NAME"
fi

KEY_NAME="${PREFIX}-compliance-key"
KEY_ID="$(aws apigateway get-api-keys --name-query "$KEY_NAME" --query 'items[0].id' --output text | none_to_empty)"
if [[ -z "$KEY_ID" ]]; then
  KEY_ID="$(aws apigateway create-api-key --name "$KEY_NAME" --enabled \
      --description "FinTrust compliance team" --query id --output text)"
  echo "Created API key $KEY_NAME"
fi
if ! OUTPUT="$(aws apigateway create-usage-plan-key --usage-plan-id "$PLAN_ID" \
      --key-id "$KEY_ID" --key-type API_KEY 2>&1)"; then
  if [[ "$OUTPUT" != *"ConflictException"* ]]; then
    echo "$OUTPUT" >&2; exit 1
  fi
fi

API_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/${STAGE}"
echo "Saving setting:"
put_param api_url "$API_URL"

echo
echo "API ready:  $API_URL"
echo
echo "The API key is a secret, so it is not printed or stored in a file."
echo "To use it in this terminal:"
echo "    source ./scripts/api_env.sh"
echo "    curl -s -H \"x-api-key: \$API_KEY\" \"\$API_URL/health\""
