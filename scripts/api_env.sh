# Put the API address, the API key and a signed-in ID token into this terminal
# session's environment.
#
#     source ./scripts/api_env.sh
#     curl -s -H "x-api-key: $API_KEY" -H "Authorization: $ID_TOKEN" "$API_URL/transactions"
#
# Must be run with "source" (not executed), so the variables stay in your shell.
# Nothing is written to disk. The token is the automation user's (signed in with
# its password and MFA code, see scripts/api_auth.py); it lasts 60 minutes, and
# its session ends after 15 minutes without a request: source this file again then.

export AWS_PAGER=""
_FINTRUST_REGION="${AWS_REGION:-eu-north-1}"
_FINTRUST_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

API_URL="$(aws ssm get-parameter --region "$_FINTRUST_REGION" --name /fintrust/dev/api_url \
    --query Parameter.Value --output text)"
_FINTRUST_KEY_ID="$(aws apigateway get-api-keys --region "$_FINTRUST_REGION" \
    --name-query fintrust-capstone-compliance-key --query 'items[0].id' --output text)"
API_KEY="$(aws apigateway get-api-key --region "$_FINTRUST_REGION" --api-key "$_FINTRUST_KEY_ID" \
    --include-value --query value --output text)"
ID_TOKEN="$(python3 "$_FINTRUST_SCRIPTS/api_auth.py")"
export API_URL API_KEY ID_TOKEN
unset _FINTRUST_REGION _FINTRUST_KEY_ID _FINTRUST_SCRIPTS

echo "API_URL=$API_URL"
echo "API_KEY is set (${#API_KEY} characters, not shown)"
echo "ID_TOKEN is set (${#ID_TOKEN} characters, not shown), signed in as fintrust-automation"
