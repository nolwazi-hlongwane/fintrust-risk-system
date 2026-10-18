"""Sign the verification scripts in to the FinTrust API (Week 15 Day 2).

Since sign-in was added, every route except /health needs two things:
    x-api-key      which application is calling (the API Gateway usage plan)
    Authorization  a signed-in person's Cognito ID token

The verification scripts and the end-to-end test sign in as the automation
user, which scripts/manage_users.py creates. Its password and MFA secret are
kept encrypted in SSM Parameter Store under /fintrust-admin/, readable only
with administrator credentials, and are never printed or written to disk.

    from api_auth import sign_in
    credentials = sign_in(session, api_url, api_key)
    urllib.request.Request(url, headers=credentials.headers())
"""

import json
import os
import ssl
import urllib.error
import urllib.request

from botocore.exceptions import ClientError
from totp import fresh_code

ENVIRONMENT = os.environ.get("FINTRUST_ENV", "dev")
AUTOMATION_USERNAME = "fintrust-automation"
ADMIN_PREFIX = f"/fintrust-admin/{ENVIRONMENT}/"
PASSWORD_PARAMETER = ADMIN_PREFIX + "automation_password"
TOTP_PARAMETER = ADMIN_PREFIX + "automation_totp_secret"

# Python uses its own certificate list, which on macOS can be missing. Prefer
# certifi's (installed with boto3's dependencies) when it is there.
try:
    import certifi
    TLS = ssl.create_default_context(cafile=certifi.where())
except ImportError:          # pragma: no cover
    TLS = ssl.create_default_context()


class SignInFailed(Exception):
    """The automation user could not sign in. The message says what to do."""


class Credentials:
    """What a script sends with each request."""

    def __init__(self, api_key, id_token=None, username=None):
        self.api_key = api_key
        self.id_token = id_token
        self.username = username

    def headers(self):
        found = {"x-api-key": self.api_key} if self.api_key else {}
        if self.id_token:
            found["Authorization"] = self.id_token
        return found

    def __repr__(self):                       # never show a secret in a traceback or a print
        return f"Credentials(api_key=<{len(self.api_key or '')} characters>, signed_in_as={self.username!r})"


def headers_for(key):
    """Headers for an API call. `key` is a Credentials, a bare API key (text), or None."""
    if key is None:
        return {}
    if isinstance(key, str):
        return {"x-api-key": key}
    return key.headers()


def _parameter(ssm, name):
    return ssm.get_parameter(Name=f"/fintrust/{ENVIRONMENT}/{name}")["Parameter"]["Value"]


def id_token(session, used_step=None):
    """Sign the automation user in to Cognito (password, then MFA code). Returns (ID token, code step)."""
    ssm = session.client("ssm")
    cognito = session.client("cognito-idp")
    try:
        pool_id = _parameter(ssm, "cognito_user_pool_id")
        client_id = _parameter(ssm, "cognito_automation_client_id")
        password = ssm.get_parameter(Name=PASSWORD_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
        secret = ssm.get_parameter(Name=TOTP_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound as error:
        raise SignInFailed("Sign-in is not set up yet. Run ./infrastructure/cognito/deploy_auth.sh first.") from error

    for attempt in (1, 2):
        started = cognito.admin_initiate_auth(
            UserPoolId=pool_id, ClientId=client_id, AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": AUTOMATION_USERNAME, "PASSWORD": password},
        )
        if started.get("ChallengeName") != "SOFTWARE_TOKEN_MFA":
            raise SignInFailed(f"Unexpected sign-in step {started.get('ChallengeName')}. "
                               "Run: python scripts/manage_users.py automation")
        code, step = fresh_code(secret, used_step)
        try:
            finished = cognito.admin_respond_to_auth_challenge(
                UserPoolId=pool_id, ClientId=client_id, ChallengeName="SOFTWARE_TOKEN_MFA",
                Session=started["Session"],
                ChallengeResponses={"USERNAME": AUTOMATION_USERNAME, "SOFTWARE_TOKEN_MFA_CODE": code},
            )
            return finished["AuthenticationResult"]["IdToken"], step
        except ClientError as error:
            # Cognito accepts each code once. If this one was just used (for
            # example by the set-up a moment ago), wait for the next 30 seconds.
            if attempt == 2 or error.response["Error"]["Code"] not in ("CodeMismatchException", "ExpiredCodeException",
                                                                       "NotAuthorizedException"):
                raise SignInFailed(f"The MFA code was refused: {error.response['Error']['Message']}. "
                                   "Run: python scripts/manage_users.py automation") from error
            used_step = step
    raise SignInFailed("Sign-in did not complete")      # pragma: no cover


def _send(url, method, headers):
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30, context=TLS) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode("utf-8") or "null")
        except ValueError:
            return error.code, None


def sign_in(session, api_url, api_key):
    """Sign in and start an API session. Returns Credentials for every later call."""
    token, _step = id_token(session)
    credentials = Credentials(api_key, token, AUTOMATION_USERNAME)
    status, body = _send(f"{api_url.rstrip('/')}/session", "POST", credentials.headers())
    if status not in (200, 201):
        raise SignInFailed(f"POST /session answered {status}: {body}")
    return credentials


def sign_out(api_url, credentials):
    """End the API session. The same token is refused from then on."""
    return _send(f"{api_url.rstrip('/')}/session", "DELETE", credentials.headers())[0]


def main():
    """Sign in and print the ID token, for scripts/api_env.sh (curl from a terminal)."""
    import sys

    import boto3

    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "eu-north-1"))
    ssm = session.client("ssm")
    apigateway = session.client("apigateway")
    api_url = _parameter(ssm, "api_url")
    key_id = apigateway.get_api_keys(nameQuery="fintrust-capstone-compliance-key")["items"][0]["id"]
    key = apigateway.get_api_key(apiKey=key_id, includeValue=True)["value"]
    try:
        credentials = sign_in(session, api_url, key)
    except SignInFailed as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(credentials.id_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
