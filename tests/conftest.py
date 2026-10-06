"""Shared pytest fixtures.

moto replaces AWS with an in-memory fake, so these tests never touch a real
account and cost nothing. Fake credentials are set BEFORE any fintrust module
is imported, so a module-level boto3 client can never pick up real ones.
"""

import os
from pathlib import Path

os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "eu-north-1"
os.environ["AWS_REGION"] = "eu-north-1"
os.environ["FINTRUST_ENV"] = "test"

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from fintrust import config  # noqa: E402

REGION = "eu-north-1"
SSM_PREFIX = "/fintrust/test/"


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    """Start every test with no FINTRUST_ settings and an empty SSM cache."""
    for key in list(os.environ):
        if key.startswith("FINTRUST_") and key != "FINTRUST_ENV":
            monkeypatch.delenv(key, raising=False)
    config.reset_cache()
    yield
    config.reset_cache()


@pytest.fixture
def aws():
    """Turn on the moto fake for every AWS service used in a test."""
    with mock_aws():
        yield


@pytest.fixture
def ssm(aws):
    """A fake SSM Parameter Store, empty."""
    return boto3.client("ssm", region_name=REGION)


@pytest.fixture
def put_parameter(ssm):
    """Helper: put_parameter("raw_bucket", "my-bucket")."""

    def _put(name, value, secure=False):
        ssm.put_parameter(
            Name=f"{SSM_PREFIX}{name}",
            Value=value,
            Type="SecureString" if secure else "String",
            Overwrite=True,
        )

    return _put


# ----------------------------------------------------------------------------
# DynamoDB (moto)
# ----------------------------------------------------------------------------
TABLE_NAME = "fintrust-capstone-events"


@pytest.fixture
def events_table(aws, monkeypatch):
    """A fake events table with the same keys and index as the deployed one
    (see infrastructure/dynamodb/deploy_table.sh)."""
    from fintrust import dynamo

    monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
    monkeypatch.setenv("FINTRUST_DYNAMO_TABLE", TABLE_NAME)
    dynamo.reset()
    resource = boto3.resource("dynamodb", region_name=REGION)
    created = resource.create_table(
        TableName=TABLE_NAME,
        AttributeDefinitions=[
            {"AttributeName": "account_id", "AttributeType": "S"},
            {"AttributeName": "tx_timestamp", "AttributeType": "S"},
            {"AttributeName": "transaction_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "account_id", "KeyType": "HASH"},
            {"AttributeName": "tx_timestamp", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by_transaction",
                "KeySchema": [
                    {"AttributeName": "transaction_id", "KeyType": "HASH"},
                    {"AttributeName": "tx_timestamp", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    yield created
    dynamo.reset()


# ----------------------------------------------------------------------------
# PostgreSQL (integration tests only)
#
# Integration tests need a real PostgreSQL, because moto cannot fake a database
# engine. They read the standard PGHOST / PGPORT / PGUSER / PGPASSWORD variables:
#   locally:  docker compose up -d   (see docker-compose.yml)
#   in CI:    a postgres service container, on the develop branch only
# Without PGHOST they are skipped, unless REQUIRE_POSTGRES=1 (set in CI so a
# missing database fails the build instead of silently skipping).
# ----------------------------------------------------------------------------
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
TEST_DB_NAME = "fintrust_test"
TEST_USER_PASSWORD = "local-test-only"


def _admin_connect(dbname):
    import psycopg2

    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
        dbname=dbname,
        connect_timeout=5,
    )


@pytest.fixture(scope="session")
def postgres_database():
    """Create a fresh database and load schema, users and seed data once."""
    if not os.environ.get("PGHOST"):
        if os.environ.get("REQUIRE_POSTGRES") == "1":
            pytest.fail("REQUIRE_POSTGRES=1 but PGHOST is not set")
        pytest.skip("PostgreSQL not configured (set PGHOST to run integration tests)")

    server = _admin_connect("postgres")
    server.autocommit = True
    with server.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
        cursor.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    server.close()

    database = _admin_connect(TEST_DB_NAME)
    database.autocommit = True
    with database.cursor() as cursor:
        for name in ("schema.sql", "db_users.sql", "seed.sql"):
            cursor.execute((SQL_DIR / name).read_text())
        for user in ("fintrust_score", "fintrust_query", "fintrust_report"):
            cursor.execute(f"ALTER ROLE {user} PASSWORD %s", (TEST_USER_PASSWORD,))
    database.close()
    return TEST_DB_NAME


@pytest.fixture
def admin(postgres_database):
    """A superuser connection for checking results and cleaning up."""
    connection = _admin_connect(postgres_database)
    connection.autocommit = True
    yield connection
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM alerts WHERE transaction_id LIKE 'TXN-TEST-%'")
        cursor.execute("DELETE FROM risk_events WHERE transaction_id LIKE 'TXN-TEST-%'")
        cursor.execute("DELETE FROM transactions WHERE transaction_id LIKE 'TXN-TEST-%'")
    connection.close()


@pytest.fixture
def as_db_user(postgres_database, monkeypatch):
    """Point fintrust.db at the test database as a given user.

        as_db_user("fintrust_score")
    """
    from fintrust import db

    def _use(user):
        db.close_connection()
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_DB_AUTH_MODE", "password")
        monkeypatch.setenv("FINTRUST_DB_HOST", os.environ["PGHOST"])
        monkeypatch.setenv("FINTRUST_DB_PORT", os.environ.get("PGPORT", "5432"))
        monkeypatch.setenv("FINTRUST_DB_NAME", postgres_database)
        monkeypatch.setenv("FINTRUST_DB_USER", user)
        monkeypatch.setenv("FINTRUST_DB_PASSWORD", TEST_USER_PASSWORD)

    yield _use
    db.close_connection()
