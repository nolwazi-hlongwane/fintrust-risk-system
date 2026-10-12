# Put the API address and key into this terminal session's environment.
#
#     source ./scripts/api_env.sh
#     curl -s -H "x-api-key: $API_KEY" "$API_URL/health"
#
# Must be run with "source" (not executed), so the variables stay in your shell.
# The key is fetched from AWS each time and never written to disk.

export AWS_PAGER=""
_FINTRUST_REGION="${AWS_REGION:-eu-north-1}"

API_URL="$(aws ssm get-parameter --region "$_FINTRUST_REGION" --name /fintrust/dev/api_url \
    --query Parameter.Value --output text)"
_FINTRUST_KEY_ID="$(aws apigateway get-api-keys --region "$_FINTRUST_REGION" \
    --name-query fintrust-capstone-compliance-key --query 'items[0].id' --output text)"
API_KEY="$(aws apigateway get-api-key --region "$_FINTRUST_REGION" --api-key "$_FINTRUST_KEY_ID" \
    --include-value --query value --output text)"
export API_URL API_KEY
unset _FINTRUST_REGION _FINTRUST_KEY_ID

echo "API_URL=$API_URL"
echo "API_KEY is set (${#API_KEY} characters, not shown)"
