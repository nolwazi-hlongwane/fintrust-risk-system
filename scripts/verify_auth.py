"""Week 15 Day 2 verifier: sign-in, roles and sessions on the real account.

    python scripts/verify_auth.py

Checks the settings first, then proves the behaviour with real requests:
    * the user pool: MFA required, no self sign-up, strong passwords, deletion protection
    * the dashboard's app client: code flow with PKCE only, no secret, short tokens,
      token revocation, the exact return address
    * managed login on the sign-in domain
    * the session table and its time-to-live
    * API Gateway: every route needs the API key, and every route except /health
      needs a Cognito sign-in
    * live: no sign-in is refused; signed in it works; after signing out the SAME
      token is refused at once, although it has not expired

The database must be running (./scripts/rds_start.sh). No tunnel is needed.
Finishes with a line such as "27 of 27 checks passed".
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from api_auth import TLS, Credentials, SignInFailed, headers_for, sign_in, sign_out
from botocore.exceptions import ClientError
from verify_write_api_alerts import PREFIX, REGION, account_id, api_key, check, parameter, results, session

cognito = session.client("cognito-idp")
apigateway = session.client("apigateway")
dynamodb = session.client("dynamodb")


def call(url, method, path, key=None):
    request = urllib.request.Request(url + path, method=method, headers=headers_for(key))
    try:
        with urllib.request.urlopen(request, timeout=30, context=TLS) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8")
        try:
            return error.code, json.loads(text or "null")
        except ValueError:
            return error.code, None


def verify_user_pool():
    print("\nUser pool")
    pool_id = parameter("cognito_user_pool_id")
    pool = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    policy = pool["Policies"]["PasswordPolicy"]
    check("MFA is required for everyone, by authenticator app",
          cognito.get_user_pool_mfa_config(UserPoolId=pool_id).get("MfaConfiguration") == "ON"
          and cognito.get_user_pool_mfa_config(UserPoolId=pool_id)["SoftwareTokenMfaConfiguration"]["Enabled"])
    check("nobody can sign themselves up: only an administrator creates accounts",
          pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True)
    rules = ("RequireUppercase", "RequireLowercase", "RequireNumbers", "RequireSymbols")
    check("passwords: 12 or more characters, mixed case, a number and a symbol",
          policy["MinimumLength"] >= 12 and all(policy[rule] for rule in rules))
    check("the user pool cannot be deleted by accident", pool.get("DeletionProtection") == "ACTIVE")
    check("Essentials plan (managed login; 10 000 monthly active users free)", pool.get("UserPoolTier") == "ESSENTIALS",
          pool.get("UserPoolTier"))
    groups = {group["GroupName"] for group in cognito.list_groups(UserPoolId=pool_id)["Groups"]}
    check("the two roles exist as groups", {"compliance-officer", "compliance-analyst"} <= groups, ", ".join(groups))

    client = cognito.describe_user_pool_client(UserPoolId=pool_id,
                                               ClientId=parameter("cognito_client_id"))["UserPoolClient"]
    return_url = parameter("dashboard_url").rstrip("/") + "/"
    check("dashboard client: no secret (a browser cannot keep one)", "ClientSecret" not in client)
    check("dashboard client: authorization code flow only (no implicit flow), with openid scope",
          client.get("AllowedOAuthFlows") == ["code"] and "openid" in client.get("AllowedOAuthScopes", []))
    check("dashboard client: returns only to the dashboard's own address",
          client.get("CallbackURLs") == [return_url] and client.get("LogoutURLs") == [return_url], return_url)
    units = client.get("TokenValidityUnits", {})
    check("dashboard client: ID token 30 minutes, refresh token 8 hours",
          client.get("IdTokenValidity") == 30 and units.get("IdToken") == "minutes"
          and client.get("RefreshTokenValidity") == 8 and units.get("RefreshToken") == "hours")
    check("dashboard client: tokens can be revoked, and sign-in errors do not reveal usernames",
          client.get("EnableTokenRevocation") is True and client.get("PreventUserExistenceErrors") == "ENABLED")

    domain = pool.get("Domain")
    description = cognito.describe_user_pool_domain(Domain=domain)["DomainDescription"] if domain else {}
    check("the sign-in pages are Cognito managed login (version 2)", description.get("ManagedLoginVersion") == 2,
          parameter("cognito_domain"))


def verify_session_table():
    print("\nSession table (DynamoDB)")
    name = parameter("sessions_table")
    table = dynamodb.describe_table(TableName=name)["Table"]
    check("the table exists, keyed by session_id",
          [key["AttributeName"] for key in table["KeySchema"]] == ["session_id"], name)
    ttl = dynamodb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
    check("old sign-in records delete themselves (time-to-live on expires_at)",
          ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING") and ttl.get("AttributeName") == "expires_at")
    return name


def verify_gateway():
    print("\nAPI Gateway")
    apis = apigateway.get_rest_apis(limit=500)["items"]
    api_id = next(item["id"] for item in apis if item["name"] == f"{PREFIX}-api")
    authorizers = apigateway.get_authorizers(restApiId=api_id)["items"]
    cognito_authorizers = [item for item in authorizers if item["type"] == "COGNITO_USER_POOLS"]
    check("a Cognito authorizer checks this user pool",
          len(cognito_authorizers) == 1
          and cognito_authorizers[0]["providerARNs"] == [parameter("cognito_user_pool_arn")])
    open_routes, key_free = [], []
    for resource in apigateway.get_resources(restApiId=api_id, limit=500)["items"]:
        for method in resource.get("resourceMethods", {}):
            if method == "OPTIONS":
                continue
            settings = apigateway.get_method(restApiId=api_id, resourceId=resource["id"], httpMethod=method)
            if not settings.get("apiKeyRequired"):
                key_free.append(f"{method} {resource['path']}")
            if resource["path"] != "/health" and settings.get("authorizationType") != "COGNITO_USER_POOLS":
                open_routes.append(f"{method} {resource['path']}")
    check("every route needs the API key", not key_free, ", ".join(key_free))
    check("every route except GET /health needs a Cognito sign-in", not open_routes, ", ".join(open_routes))


def verify_live(url, table):
    print("\nLive requests")
    key = api_key()
    status, body = call(url, "GET", "/transactions", key)
    check("with the API key but no sign-in: refused (401)", status == 401, f"HTTP {status}")
    status, _ = call(url, "GET", "/transactions", Credentials(key, "eyJhbGciOiJSUzI1NiJ9.e30.not-a-real-signature"))
    check("with a forged token: refused (401)", status == 401, f"HTTP {status}")

    try:
        credentials = sign_in(session, url, key)
    except SignInFailed as error:
        check("the automation user signs in with password and MFA code", False, str(error))
        return
    check("the automation user signs in with password and MFA code, and a session starts", True)
    status, me = call(url, "GET", "/session", credentials)
    check("GET /session names the person, the role and the permissions",
          status == 200 and me.get("roles") == ["compliance-officer"] and me["permissions"]["resolve_alerts"],
          f"HTTP {status}")
    status, _ = call(url, "GET", "/transactions?" + urllib.parse.urlencode({"sort": "amount_desc", "limit": 5}),
                     credentials)
    check("signed in: GET /transactions, sorted by amount, answers 200", status == 200, f"HTTP {status}")
    status, _ = call(url, "GET", "/transactions", Credentials(None, credentials.id_token))
    check("a valid sign-in without the API key is still refused by the usage plan (403)", status == 403,
          f"HTTP {status}")

    status = sign_out(url, credentials)
    check("DELETE /session signs out", status == 200, f"HTTP {status}")
    status, body = call(url, "GET", "/transactions", credentials)
    reason = ((body or {}).get("error") or {}).get("reason")
    check("after signing out, the SAME unexpired token is refused at once (401 SESSION_ENDED)",
          status == 401 and reason == "SIGNED_OUT", f"HTTP {status} {reason}")
    items = dynamodb.scan(TableName=table, FilterExpression="username = :u AND end_reason = :r",
                          ExpressionAttributeValues={":u": {"S": "fintrust-automation"},
                                                     ":r": {"S": "SIGNED_OUT"}})["Items"]
    check("the session record says how it ended, and holds no token", bool(items)
          and not any(field in items[0] for field in ("id_token", "token", "password")))


def main():
    print(f"Verifying sign-in in account {account_id}, region {REGION}")
    try:
        url = parameter("api_url")
        verify_user_pool()
        table = verify_session_table()
        verify_gateway()
        status, health = call(url, "GET", "/health", api_key())
        if status != 200:
            check("the API can reach the database", False, f"HTTP {status} {health}")
            print("\nStopping: is the database running?  ./scripts/rds_start.sh")
        else:
            verify_live(url, table)
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        print("Has sign-in been deployed?  ./infrastructure/cognito/deploy_auth.sh", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
