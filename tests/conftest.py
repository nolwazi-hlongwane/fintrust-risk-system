"""Shared pytest fixtures.

moto replaces AWS with an in-memory fake, so these tests never touch a real
account and cost nothing. Fake credentials are set BEFORE any fintrust module
is imported, so a module-level boto3 client can never pick up real ones.
"""

import os

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
