"""Save the project's Logs Insights queries in CloudWatch, so they are one click away.

    python infrastructure/monitoring/save_queries.py

The queries live in queries.json (next to this file) and are documented in
docs/logs-insights-queries.md. In the console they appear under
CloudWatch > Logs Insights > Saved and sample queries > FinTrust.

Safe to run more than once: an existing query with the same name is updated.
Saved queries are free.
"""

import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REGION = os.environ.get("AWS_REGION", "eu-north-1")
PREFIX = "fintrust-capstone"
FOLDER = "FinTrust"                      # a "/" in the name becomes a folder in the console
QUERIES = Path(__file__).resolve().parent / "queries.json"


def main():
    logs = boto3.client("logs", region_name=REGION)
    try:
        existing = {}
        # The prefix may not end with "/", so ask for "FinTrust" and keep "FinTrust/..." names.
        arguments = {"queryDefinitionNamePrefix": FOLDER}
        while True:
            page = logs.describe_query_definitions(**arguments)
            existing.update({item["name"]: item["queryDefinitionId"] for item in page["queryDefinitions"]
                             if item["name"].startswith(f"{FOLDER}/")})
            if "nextToken" not in page:
                break
            arguments["nextToken"] = page["nextToken"]

        for query in json.loads(QUERIES.read_text()):
            name = f"{FOLDER}/{query['name']}"
            arguments = {
                "name": name,
                "queryString": query["query"],
                "logGroupNames": [f"/aws/lambda/{PREFIX}-{function}" for function in query["functions"]],
            }
            if name in existing:
                arguments["queryDefinitionId"] = existing[name]
            logs.put_query_definition(**arguments)
            print(f"  {'Updated' if name in existing else 'Saved  '}  {name}")
    except (ClientError, BotoCoreError) as error:
        print(f"ERROR: could not save the queries: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
