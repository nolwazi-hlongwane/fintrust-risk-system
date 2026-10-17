#!/usr/bin/env bash
# Week 15 Day 2: sign-in for the dashboard and the API, with Amazon Cognito.
#
#   Person --password + 6-digit code--> Cognito managed login (hosted by AWS)
#          <--ID token (a signed JWT)--
#   Dashboard --x-api-key + ID token--> API Gateway (checks both) --> query Lambda
#                                                                   (checks session + role)
#
# Creates (or brings up to date):
#   User pool    fintrust-capstone-users (Essentials plan: 10 000 sign-ins a month free)
#                - only an administrator can create users: there is no self sign-up
#                - passwords: at least 12 characters with upper and lower case, a
#                  number and a symbol; the last 5 cannot be reused
#                - MFA is REQUIRED for everyone: an authenticator app (TOTP) code
#                  at every sign-in. Free; no SMS, which costs money and is weaker
#                - sign-in errors never reveal whether a username exists
#                - deletion protection: the pool cannot be deleted by accident
#   Groups       compliance-officer  read, upload, resolve alerts, flag accounts
#                compliance-analyst  read and upload only
#   Domain       fintrust-capstone-<account>.auth.<region>.amazoncognito.com, with
#                managed login: the sign-in, password and MFA pages are hosted by
#                AWS, so the dashboard's own code never handles a password
#   App clients  dashboard   authorization code flow with PKCE, no client secret
#                            (a browser cannot keep one), tokens valid 30 minutes,
#                            refresh token 8 hours, token revocation on
#                automation  for the verification scripts and the end-to-end
#                            test only: signs in with the administrator's IAM
#                            credentials (ADMIN_USER_PASSWORD_AUTH), never from a browser
#
# Then signs up the automation user (scripts/manage_users.py automation).
#
# Needs the dashboard's address (deploy_frontend.sh) for the sign-in and sign-out
# return addresses. Safe to run more than once.
#
# Usage:  ./infrastructure/cognito/deploy_auth.sh

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
source "$HERE/../common.sh"

DASHBOARD_URL="$(aws ssm get-parameter --name "/fintrust/${FINTRUST_ENV}/dashboard_url" \
    --query Parameter.Value --output text 2>/dev/null || true)"
if [[ -z "$DASHBOARD_URL" ]]; then
  echo "ERROR: the dashboard's address is not in SSM. Run ./infrastructure/frontend/deploy_frontend.sh first." >&2
  exit 1
fi
RETURN_URL="${DASHBOARD_URL%/}/"     # Cognito compares return addresses exactly, including the final /
WORK="$ROOT/build/cognito"
mkdir -p "$WORK"
sed "s|__DASHBOARD_URL__|${RETURN_URL}|g" "$HERE/invitation.json" > "$WORK/invitation.json"

# ---------- 1. user pool ----------
POOL_ID="$(aws cognito-idp list-user-pools --max-results 60 \
    --query "UserPools[?Name=='${USER_POOL_NAME}'].Id | [0]" --output text | none_to_empty)"
if [[ -z "$POOL_ID" ]]; then
  POOL_ID="$(aws cognito-idp create-user-pool --pool-name "$USER_POOL_NAME" \
      --user-pool-tier ESSENTIALS \
      --deletion-protection ACTIVE \
      --username-configuration CaseSensitive=false \
      --auto-verified-attributes email \
      --user-attribute-update-settings AttributesRequireVerificationBeforeUpdate=email \
      --account-recovery-setting 'RecoveryMechanisms=[{Priority=1,Name=verified_email}]' \
      --policies "file://$HERE/password-policy.json" \
      --admin-create-user-config "file://$WORK/invitation.json" \
      --user-pool-tags Project=fintrust-capstone \
      --query UserPool.Id --output text)"
  echo "Created user pool $USER_POOL_NAME ($POOL_ID)"
else
  # Not changed on later runs: update-user-pool resets every setting it is not
  # given, which is too easy to get wrong on a pool that already has users.
  echo "User pool $USER_POOL_NAME exists ($POOL_ID)"
fi
POOL_ARN="arn:aws:cognito-idp:${AWS_REGION}:${ACCOUNT_ID}:userpool/${POOL_ID}"

# MFA required for everyone, by authenticator app.
aws cognito-idp set-user-pool-mfa-config --user-pool-id "$POOL_ID" \
    --software-token-mfa-configuration Enabled=true --mfa-configuration ON >/dev/null
echo "MFA: required, authenticator app"

# ---------- 2. groups (the roles) ----------
make_group() {   # make_group <name> <precedence> <description>
  if ! aws cognito-idp get-group --user-pool-id "$POOL_ID" --group-name "$1" >/dev/null 2>&1; then
    aws cognito-idp create-group --user-pool-id "$POOL_ID" --group-name "$1" \
        --precedence "$2" --description "$3" >/dev/null
    echo "Created group $1"
  fi
}
make_group compliance-officer 1 "Reads everything, uploads files, resolves alerts and flags accounts"
make_group compliance-analyst 2 "Reads everything and uploads files; cannot resolve alerts or flag accounts"

# ---------- 3. app clients ----------
# update-user-pool-client resets every setting it is not given, so the whole
# set is passed every time, for creating and for updating alike.
client_id() {   # client_id <name>
  aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --max-results 60 \
      --query "UserPoolClients[?ClientName=='$1'].ClientId | [0]" --output text | none_to_empty
}

DASHBOARD_CLIENT_NAME="${PREFIX}-dashboard"
DASHBOARD_SETTINGS=(
  --refresh-token-validity 8 --access-token-validity 30 --id-token-validity 30
  --token-validity-units AccessToken=minutes,IdToken=minutes,RefreshToken=hours
  --auth-session-validity 5
  --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH
  --supported-identity-providers COGNITO
  --callback-urls "$RETURN_URL" --logout-urls "$RETURN_URL"
  --allowed-o-auth-flows code --allowed-o-auth-scopes openid email profile
  --allowed-o-auth-flows-user-pool-client
  --read-attributes email email_verified name
  --prevent-user-existence-errors ENABLED --enable-token-revocation
)
DASHBOARD_CLIENT_ID="$(client_id "$DASHBOARD_CLIENT_NAME")"
if [[ -z "$DASHBOARD_CLIENT_ID" ]]; then
  DASHBOARD_CLIENT_ID="$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" \
      --client-name "$DASHBOARD_CLIENT_NAME" --no-generate-secret "${DASHBOARD_SETTINGS[@]}" \
      --query UserPoolClient.ClientId --output text)"
  echo "Created app client $DASHBOARD_CLIENT_NAME"
else
  aws cognito-idp update-user-pool-client --user-pool-id "$POOL_ID" --client-id "$DASHBOARD_CLIENT_ID" \
      --client-name "$DASHBOARD_CLIENT_NAME" "${DASHBOARD_SETTINGS[@]}" >/dev/null
  echo "Updated app client $DASHBOARD_CLIENT_NAME"
fi
echo "  sign-in returns to $RETURN_URL"

AUTOMATION_CLIENT_NAME="${PREFIX}-automation"
AUTOMATION_SETTINGS=(
  --refresh-token-validity 1 --access-token-validity 60 --id-token-validity 60
  --token-validity-units AccessToken=minutes,IdToken=minutes,RefreshToken=hours
  --explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH
  --read-attributes email name
  --prevent-user-existence-errors ENABLED --enable-token-revocation
)
AUTOMATION_CLIENT_ID="$(client_id "$AUTOMATION_CLIENT_NAME")"
if [[ -z "$AUTOMATION_CLIENT_ID" ]]; then
  AUTOMATION_CLIENT_ID="$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" \
      --client-name "$AUTOMATION_CLIENT_NAME" --no-generate-secret "${AUTOMATION_SETTINGS[@]}" \
      --query UserPoolClient.ClientId --output text)"
  echo "Created app client $AUTOMATION_CLIENT_NAME"
else
  aws cognito-idp update-user-pool-client --user-pool-id "$POOL_ID" --client-id "$AUTOMATION_CLIENT_ID" \
      --client-name "$AUTOMATION_CLIENT_NAME" "${AUTOMATION_SETTINGS[@]}" >/dev/null
  echo "Updated app client $AUTOMATION_CLIENT_NAME"
fi

# ---------- 4. domain with managed login ----------
DOMAIN_PREFIX="${PREFIX}-${ACCOUNT_ID}"            # globally unique; may not contain "aws" or "cognito"
CURRENT_DOMAIN="$(aws cognito-idp describe-user-pool --user-pool-id "$POOL_ID" \
    --query UserPool.Domain --output text | none_to_empty)"
if [[ -z "$CURRENT_DOMAIN" ]]; then
  aws cognito-idp create-user-pool-domain --user-pool-id "$POOL_ID" --domain "$DOMAIN_PREFIX" \
      --managed-login-version 2 >/dev/null
  echo "Created sign-in domain $DOMAIN_PREFIX"
  CURRENT_DOMAIN="$DOMAIN_PREFIX"
fi
VERSION="$(aws cognito-idp describe-user-pool-domain --domain "$CURRENT_DOMAIN" \
    --query DomainDescription.ManagedLoginVersion --output text | none_to_empty)"
if [[ "$VERSION" != "2" ]]; then
  aws cognito-idp update-user-pool-domain --user-pool-id "$POOL_ID" --domain "$CURRENT_DOMAIN" \
      --managed-login-version 2 >/dev/null
  echo "Sign-in domain switched to managed login"
fi
SIGN_IN_URL="https://${CURRENT_DOMAIN}.auth.${AWS_REGION}.amazoncognito.com"

# Managed login pages need a branding style for the app client. Start from
# Cognito's own default style; scripts/brand_signin.py (run after the settings
# are saved, below) then gives it the dashboard's colours, logo and tab icon.
if ! BRANDING_ID="$(aws cognito-idp describe-managed-login-branding-by-client --user-pool-id "$POOL_ID" \
      --client-id "$DASHBOARD_CLIENT_ID" --query ManagedLoginBranding.ManagedLoginBrandingId \
      --output text 2>/dev/null)" || [[ -z "$BRANDING_ID" || "$BRANDING_ID" == "None" ]]; then
  aws cognito-idp create-managed-login-branding --user-pool-id "$POOL_ID" \
      --client-id "$DASHBOARD_CLIENT_ID" --use-cognito-provided-values >/dev/null
  echo "Created the sign-in page style"
fi

# ---------- 5. settings for the API, the dashboard and the scripts ----------
echo "Saving settings:"
put_param cognito_user_pool_id "$POOL_ID"
put_param cognito_user_pool_arn "$POOL_ARN"
put_param cognito_domain "$SIGN_IN_URL"
put_param cognito_client_id "$DASHBOARD_CLIENT_ID"
put_param cognito_automation_client_id "$AUTOMATION_CLIENT_ID"

# The sign-in page in the dashboard's colours. Not fatal: sign-in works either way.
python3 "$ROOT/scripts/brand_signin.py" || echo "Run python scripts/brand_signin.py again later to restyle the sign-in page."

# ---------- 6. the automation user (verification scripts and end-to-end test) ----------
python3 "$ROOT/scripts/manage_users.py" automation

echo
echo "Sign-in is ready:  $SIGN_IN_URL"
echo "Next:"
echo "  1. Create your own account (an invitation with a temporary password is emailed to you)."
echo "     The options are listed by:  python scripts/manage_users.py create --help  (README, Step 11, has an example)"
echo "  2. Publish the API and the dashboard with sign-in switched on:"
echo "       ./infrastructure/apigateway/deploy_api.sh && ./infrastructure/frontend/deploy_frontend.sh"
