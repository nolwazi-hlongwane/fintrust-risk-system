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


ROLE_POLICY_FILES = sorted(POLICY_DIR.glob("*-base.json")) + sorted(POLICY_DIR.glob("*-data.json"))


@pytest.mark.parametrize("path", ROLE_POLICY_FILES, ids=lambda p: p.name)
def test_every_role_action_is_inside_the_permission_boundary(path):
    boundary_actions = set()
    for statement in statements(POLICY_DIR / "permission-boundary.json"):
        boundary_actions.update(as_list(statement["Action"]))
    for statement in statements(path):
        for action in as_list(statement["Action"]):
            assert action in boundary_actions, f"{path.name}: {action} is outside the boundary"


@pytest.mark.parametrize("path", sorted(POLICY_DIR.glob("*-data.json")), ids=lambda p: p.name)
def test_data_policies_never_use_resource_star(path):
    for statement in statements(path):
        for resource in as_list(statement["Resource"]):
            assert resource != "*"
            assert not resource.endswith(":*"), f"{path.name}: {resource} is too broad"


@pytest.mark.parametrize("function", ["score", "query", "report"])
def test_each_vpc_function_can_log_in_only_as_its_own_database_user(function):
    connect = [
        s for s in statements(POLICY_DIR / f"{function}-data.json")
        if "rds-db:connect" in as_list(s["Action"])
    ]
    assert len(connect) == 1
    assert connect[0]["Resource"].endswith(f":dbuser:__DBI_RESOURCE_ID__/fintrust_{function}")


@pytest.mark.parametrize("function", ["ingest", "alert"])
def test_non_vpc_functions_cannot_log_in_to_the_database(function):
    for path in POLICY_DIR.glob(f"{function}-*.json"):
        for statement in statements(path):
            assert "rds-db:connect" not in as_list(statement["Action"])


def test_only_ingest_and_score_can_write_to_dynamodb_so_far():
    writers = set()
    for path in ROLE_POLICY_FILES:
        for statement in statements(path):
            if {"dynamodb:PutItem", "dynamodb:UpdateItem"} & set(as_list(statement["Action"])):
                writers.add(path.name.split("-")[0])
    assert writers == {"ingest", "score"}


def test_query_function_has_read_only_dynamodb_access():
    actions = set()
    for statement in statements(POLICY_DIR / "query-data.json"):
        actions.update(a for a in as_list(statement["Action"]) if a.startswith("dynamodb:"))
    assert actions == {"dynamodb:Query", "dynamodb:GetItem"}


# ---- Week 13 Day 3: buckets, queues and the encryption key -------------------

def actions_on(function, resource_fragment):
    """Every action the function's data policy allows on resources containing the fragment."""
    found = set()
    for statement in statements(POLICY_DIR / f"{function}-data.json"):
        if any(resource_fragment in resource for resource in as_list(statement["Resource"])):
            found.update(as_list(statement["Action"]))
    return found


def test_ingest_can_read_raw_but_not_write_it():
    assert actions_on("ingest", "-raw-") == {"s3:GetObject"}


def test_ingest_can_write_processed_and_quarantine_but_not_read_them():
    assert actions_on("ingest", "-processed-") == {"s3:PutObject"}
    assert actions_on("ingest", "-quarantine-") == {"s3:PutObject"}


def test_ingest_cannot_touch_the_reports_bucket():
    assert actions_on("ingest", "-reports-") == set()


def test_no_role_can_delete_objects_or_list_buckets():
    for path in ROLE_POLICY_FILES:
        for statement in statements(path):
            for action in as_list(statement["Action"]):
                assert action not in {"s3:DeleteObject", "s3:ListBucket", "s3:DeleteBucket"}, path.name


def test_only_ingest_sends_to_the_queue_and_only_score_receives():
    assert actions_on("ingest", "transactions.fifo") == {"sqs:SendMessage"}
    assert actions_on("score", "transactions.fifo") == {
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
    }
    for function in ("query", "report"):
        assert actions_on(function, ":sqs:") == set()


def test_no_role_can_purge_or_delete_a_queue():
    for path in ROLE_POLICY_FILES:
        for statement in statements(path):
            for action in as_list(statement["Action"]):
                assert action not in {"sqs:PurgeQueue", "sqs:DeleteQueue"}, path.name


def test_kms_use_is_limited_to_the_project_key():
    for path in sorted(POLICY_DIR.glob("*-data.json")):
        for statement in statements(path):
            if any(action.startswith("kms:") for action in as_list(statement["Action"])):
                assert statement["Resource"] == "__KMS_KEY_ARN__", path.name


def test_score_can_decrypt_but_not_create_data_keys():
    assert actions_on("score", "__KMS_KEY_ARN__") == {"kms:Decrypt"}


def test_no_role_can_manage_the_key():
    allowed = {"kms:Decrypt", "kms:GenerateDataKey"}
    for path in ROLE_POLICY_FILES:
        for statement in statements(path):
            for action in as_list(statement["Action"]):
                if action.startswith("kms:"):
                    assert action in allowed, f"{path.name}: {action}"
