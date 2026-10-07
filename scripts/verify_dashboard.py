"""Verify Week 14 Day 4 (part 2) on the deployed account: the web dashboard.

    python scripts/verify_dashboard.py

The database must be running (./scripts/rds_start.sh).

It checks three things:
    * hosting: the bucket is private, only CloudFront can read it, and the site
      is served over HTTPS with security headers
    * browser access (CORS): the API answers the dashboard's address and no other
    * the two things the dashboard needs that are new: GET /accounts, and a
      rule breakdown for transactions loaded as opening data

It changes nothing.
"""

import http.client
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from botocore.exceptions import ClientError
from verify_write_api_alerts import PREFIX, REGION, TLS, account_id, api_settings, check, parameter, results, session

BUCKET = f"{PREFIX}-dashboard-{account_id}"
FILES = ("index.html", "styles.css", "app.js", "config.js", "favicon.svg")
OTHER_SITE = "https://evil.example"

s3 = session.client("s3")
cloudfront = session.client("cloudfront")


def fetch(url, method="GET", headers=None):
    """Returns (status, headers with lower-case names, body text). Never follows a redirect."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(parts.netloc, timeout=30, context=TLS)
    else:
        connection = http.client.HTTPConnection(parts.netloc, timeout=30)
    try:
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection.request(method, target, headers=headers or {})
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        return response.status, {name.lower(): value for name, value in response.getheaders()}, body
    except OSError as error:
        return 0, {}, str(error)
    finally:
        connection.close()


def verify_hosting(dashboard_url, api_url):
    print("\nPrivate bucket")
    block = s3.get_public_access_block(Bucket=BUCKET)["PublicAccessBlockConfiguration"]
    check("all public access to the bucket is blocked", all(block.values()) and len(block) == 4)
    try:
        s3.get_bucket_website(Bucket=BUCKET)
        website = True
    except ClientError:
        website = False
    check("the bucket is not an S3 website (it cannot be browsed directly)", not website)

    distributions = cloudfront.list_distributions()["DistributionList"].get("Items", [])
    mine = [d for d in distributions if d["Comment"] == f"{PREFIX}-dashboard"]
    check("one CloudFront distribution serves the dashboard", len(mine) == 1)
    if not mine:
        return
    distribution = mine[0]

    policy = json.loads(s3.get_bucket_policy(Bucket=BUCKET)["Policy"])
    allows = [s for s in policy["Statement"] if s["Effect"] == "Allow"]
    check("the bucket policy lets this one distribution read, and nobody else",
          len(allows) == 1 and allows[0]["Principal"] == {"Service": "cloudfront.amazonaws.com"}
          and allows[0]["Action"] == "s3:GetObject"
          and allows[0]["Condition"]["StringEquals"]["AWS:SourceArn"] == distribution["ARN"])
    status, _, _ = fetch(f"https://{BUCKET}.s3.{REGION}.amazonaws.com/index.html")
    check("fetching the file straight from S3 is refused", status == 403, f"HTTP {status}")

    print("\nCloudFront")
    origin = distribution["Origins"]["Items"][0]
    check("the distribution is enabled and fully deployed",
          distribution["Enabled"] and distribution["Status"] == "Deployed", distribution["Status"])
    check("CloudFront signs its requests to S3 (origin access control)", bool(origin.get("OriginAccessControlId")))
    check("visitors are always moved to HTTPS",
          distribution["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https")
    status, headers, _ = fetch(dashboard_url.replace("https://", "http://") + "/")
    check("a plain http request is redirected to https",
          status in (301, 308) and headers.get("location", "").startswith("https://"), f"HTTP {status}")

    print("\nThe site itself")
    status, headers, body = fetch(dashboard_url + "/")
    check("the dashboard loads over HTTPS", status == 200 and "<title>FinTrust risk desk</title>" in body,
          f"HTTP {status}")
    loaded = {name: fetch(f"{dashboard_url}/{name}") for name in FILES}
    check("every file the page needs is served", all(result[0] == 200 for result in loaded.values()),
          ", ".join(f"{name} {result[0]}" for name, result in loaded.items()))
    check("config.js points at this project's API", api_url in loaded["config.js"][2])
    _, key = api_settings()
    check("the API key is not in any file served to the browser",
          not any(key in result[2] for result in loaded.values()))

    api_origin = "/".join(api_url.split("/")[:3])
    csp = headers.get("content-security-policy", "")
    check("Content-Security-Policy: only the page's own script, and only this API",
          "script-src 'self'" in csp and f"connect-src {api_origin}" in csp and "default-src 'none'" in csp
          and "unsafe-inline" not in csp)
    check("the page cannot be framed by another site, and HTTPS is enforced by the browser",
          headers.get("x-frame-options") == "DENY" and "max-age=" in headers.get("strict-transport-security", "")
          and headers.get("x-content-type-options") == "nosniff")


def verify_cors(dashboard_url, api_url, key):
    print("\nBrowser access to the API (CORS)")
    check("the API's allowed origin is the dashboard address, not *",
          parameter("cors_allowed_origin") == dashboard_url, parameter("cors_allowed_origin"))

    status, headers, _ = fetch(api_url + "/transactions", "OPTIONS", {
        "Origin": dashboard_url, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-api-key"})
    check("the browser's preflight check from the dashboard is allowed",
          status == 204 and headers.get("access-control-allow-origin") == dashboard_url
          and "x-api-key" in headers.get("access-control-allow-headers", ""), f"HTTP {status}")

    status, headers, _ = fetch(api_url + "/health", headers={"Origin": dashboard_url, "x-api-key": key})
    check("a request from the dashboard is answered for the dashboard",
          status == 200 and headers.get("access-control-allow-origin") == dashboard_url, f"HTTP {status}")

    status, headers, _ = fetch(api_url + "/health", headers={"Origin": OTHER_SITE, "x-api-key": key})
    allowed = headers.get("access-control-allow-origin", "")
    check("a request from another website is not given permission to read the answer",
          allowed == dashboard_url and OTHER_SITE not in allowed and allowed != "*", allowed)

    status, headers, _ = fetch(api_url + "/health", headers={"Origin": dashboard_url})
    check("a missing key is refused by API Gateway, readably for the dashboard only",
          status == 403 and headers.get("access-control-allow-origin") == dashboard_url, f"HTTP {status}")
    return status != 0


def verify_new_routes(api_url, key):
    print("\nWhat the dashboard reads")
    status, _, body = fetch(api_url + "/accounts?limit=100", headers={"x-api-key": key})
    accounts = json.loads(body).get("items", []) if status == 200 else []
    points = [item["total_risk_points"] for item in accounts]
    check("GET /accounts lists every account, highest risk first",
          status == 200 and len(accounts) >= 12 and points == sorted(points, reverse=True), f"HTTP {status}")
    check("the accounts list holds no customer names or contact details",
          bool(accounts) and not {"customer_id", "full_name", "email", "phone_number"} & set(accounts[0]))

    status, _, body = fetch(api_url + "/transactions/TXN-20260915-000225/journey", headers={"x-api-key": key})
    journey = json.loads(body) if status == 200 else {}
    score = journey.get("score") or {}
    rules = score.get("rules", [])
    check("a transaction loaded as opening data is still explained, from the ledger",
          journey.get("source") == "ledger" and score.get("risk_score") == 70
          and sum(rule["points"] for rule in rules) == 70,
          ", ".join(f"{rule['rule']} +{rule['points']}" for rule in rules))


def main():
    print(f"Verifying the dashboard in account {account_id}, region {REGION}")
    try:
        try:
            dashboard_url = parameter("dashboard_url")
        except ClientError:
            print("\nThe dashboard is not deployed yet.  ./infrastructure/frontend/deploy_frontend.sh", file=sys.stderr)
            return 1
        api_url, key = api_settings()
        print(f"\nDashboard: {dashboard_url}\nAPI:       {api_url}")
        verify_hosting(dashboard_url, api_url)
        status, _, body = fetch(api_url + "/health", headers={"x-api-key": key})
        if status != 200:
            check("the API can reach the database", False, f"HTTP {status} {body[:80]}")
            print("\nStopping: is the database running?  ./scripts/rds_start.sh")
        else:
            verify_cors(dashboard_url, api_url, key)
            verify_new_routes(api_url, key)
    except ClientError as error:
        print(f"\nStopped: AWS returned an error: {error}", file=sys.stderr)
        return 1

    failed = [name for name, passed in results if not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        return 1
    print(f"\nOpen the dashboard:  {dashboard_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
