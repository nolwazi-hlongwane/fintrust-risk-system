"""Print the environment variables for one Lambda function, as JSON for the AWS CLI.

    python infrastructure/lambda/build_env.py score

Functions OUTSIDE the VPC (ingest, alert) read their settings from SSM Parameter
Store at start-up, so they only need to know which environment they are in.

Functions INSIDE the VPC (score, query, report) have no route to SSM, because
the architecture has no NAT Gateway. For those, this script copies the current
SSM values into environment variables at deploy time. SSM stays the single
source of truth: change a value there, redeploy, and the function picks it up.

No secret is ever placed in an environment variable: the database login is a
short-lived token signed by the function's IAM role.
"""

import json
import os
import sys

import boto3

REGION = os.environ.get("AWS_REGION", "eu-north-1")
ENVIRONMENT = os.environ.get("FINTRUST_ENV", "dev")
VPC_FUNCTIONS = ("score", "query", "report")
# Settings a function has no use for are left out of its environment.
NOT_NEEDED = {
    "score": ("alert_",),
    "query": ("alert_",),
    "report": ("alert_", "queue_url"),
}


def build(function):
    variables = {"FINTRUST_ENV": ENVIRONMENT}
    if function in VPC_FUNCTIONS:
        prefix = f"/fintrust/{ENVIRONMENT}/"
        paginator = boto3.client("ssm", region_name=REGION).get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=prefix, Recursive=True):
            for parameter in page["Parameters"]:
                name = parameter["Name"][len(prefix):]
                if parameter["Type"] == "SecureString" or name.startswith(NOT_NEEDED[function]):
                    continue
                variables[f"FINTRUST_{name.upper()}"] = parameter["Value"]
        variables["FINTRUST_SSM_ENABLED"] = "false"
        variables["FINTRUST_DB_AUTH_MODE"] = "iam"
        variables["FINTRUST_DB_USER"] = f"fintrust_{function}"     # each function has its own database user
        missing = [key for key in ("FINTRUST_DB_HOST", "FINTRUST_DYNAMO_TABLE") if key not in variables]
        if missing:
            sys.exit(f"ERROR: SSM is missing settings needed by {function}: {', '.join(missing)}")
    return {"Variables": dict(sorted(variables.items()))}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: build_env.py <function>")
    print(json.dumps(build(sys.argv[1])))
