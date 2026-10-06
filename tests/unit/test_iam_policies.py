"""Guard rails for the IAM policy files.

The brief says: "least-privilege policies per Lambda function; no wildcard
actions permitted". These tests make that rule impossible to break by accident:
if anyone adds "s3:*" to a policy, CI goes red.
"""

import json
from pathlib import Path

import pytest

POLICY_DIR = Path(__file__).resolve().parents[2] / "infrastructure" / "iam" / "policies"
FUNCTIONS = ["ingest", "score", "alert", "query", "report"]

# The only statements allowed to use Resource "*", because AWS does not support
# naming a specific resource for these actions.
RESOURCE_STAR_ALLOWED = {"XRayTracing", "VpcNetworkInterfaces", "LogsAndTracing"}

POLICY_FILES = sorted(POLICY_DIR.glob("*.json"))


def as_list(value):
    return value if isinstance(value, list) else [value]


def statements(path):
    return as_list(json.loads(path.read_text())["Statement"])


def test_policy_files_exist():
    names = {path.name for path in POLICY_FILES}
    assert "trust-lambda.json" in names
    assert "permission-boundary.json" in names
    for function in FUNCTIONS:
        assert f"{function}-base.json" in names


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_no_wildcard_actions(path):
    for statement in statements(path):
        for action in as_list(statement["Action"]):
            assert "*" not in action, f"{path.name}: wildcard action {action}"


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_resource_star_only_where_aws_requires_it(path):
    for statement in statements(path):
        if "Resource" not in statement:
            continue  # trust policy
        if "*" in as_list(statement["Resource"]):
            assert statement["Sid"] in RESOURCE_STAR_ALLOWED, (
                f"{path.name}: statement {statement['Sid']} uses Resource '*'"
            )


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_every_statement_is_an_allow_with_a_sid(path):
    for statement in statements(path):
        assert statement["Effect"] == "Allow"
        assert statement.get("Sid"), f"{path.name}: statement without a Sid"


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_no_account_id_is_hardcoded(path):
    text = path.read_text()
    for token in text.replace('"', " ").replace(":", " ").split():
        assert not (token.isdigit() and len(token) == 12), f"{path.name}: account id {token}"


def test_only_lambda_can_assume_the_roles():
    statement = statements(POLICY_DIR / "trust-lambda.json")[0]
    assert statement["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"


@pytest.mark.parametrize("function", FUNCTIONS)
def test_each_role_logs_only_to_its_own_log_group(function):
    logs = [
        s for s in statements(POLICY_DIR / f"{function}-base.json")
        if s["Sid"] == "WriteOwnLogGroupOnly"
    ]
    assert len(logs) == 1
    assert logs[0]["Resource"].endswith(f"/aws/lambda/__PREFIX__-{function}:*")


@pytest.mark.parametrize("function", ["score", "query", "report"])
def test_vpc_functions_can_attach_to_the_vpc_but_cannot_read_ssm(function):
    sids = {s["Sid"] for s in statements(POLICY_DIR / f"{function}-base.json")}
    assert "VpcNetworkInterfaces" in sids
    assert "ReadOwnConfigFromParameterStore" not in sids


@pytest.mark.parametrize("function", ["ingest", "alert"])
def test_non_vpc_functions_read_ssm_but_have_no_vpc_permissions(function):
    sids = {s["Sid"] for s in statements(POLICY_DIR / f"{function}-base.json")}
    assert "ReadOwnConfigFromParameterStore" in sids
    assert "VpcNetworkInterfaces" not in sids


@pytest.mark.parametrize("function", FUNCTIONS)
def test_every_role_action_is_inside_the_permission_boundary(function):
    boundary_actions = set()
    for statement in statements(POLICY_DIR / "permission-boundary.json"):
        boundary_actions.update(as_list(statement["Action"]))
    for statement in statements(POLICY_DIR / f"{function}-base.json"):
        for action in as_list(statement["Action"]):
            assert action in boundary_actions, f"{function}: {action} is outside the boundary"
