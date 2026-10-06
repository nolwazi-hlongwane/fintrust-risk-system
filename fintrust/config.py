"""Application configuration.

Where a setting comes from, in order:

  1. An environment variable named FINTRUST_<NAME>   (e.g. FINTRUST_RAW_BUCKET)
  2. SSM Parameter Store at /fintrust/<env>/<name>    (e.g. /fintrust/dev/raw_bucket)
  3. A built-in default, only for tunable business rules (see DEFAULTS)

If none of the three has a value, ConfigError is raised. A Lambda that is
missing configuration should fail loudly at start-up, not half-way through
processing a customer's transactions.

Why two sources?
  SSM Parameter Store is the single source of truth. The Lambdas that run
  OUTSIDE the VPC (ingest, alert) read it directly. The Lambdas that run INSIDE
  the VPC (score, query, report) have no route to SSM, because the architecture
  has no NAT Gateway, so the deploy script copies the same SSM values into
  their environment variables and sets FINTRUST_SSM_ENABLED=false.

No secrets live here. The database uses IAM authentication (a short-lived
signed token), so there is no database password to configure in AWS.
"""

import os
from decimal import Decimal, InvalidOperation

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from fintrust.log import get_logger

logger = get_logger("config")

DEFAULT_REGION = "eu-north-1"  # Europe (Stockholm)

# Tunable business rules. These are safe to default because they are not
# secrets and not tied to one AWS account. Changing a value in SSM changes the
# behaviour without a code change.
DEFAULTS = {
    "high_amount_threshold": "50000",   # rand; HIGH_AMOUNT fires above this
    "high_amount_points": "50",
    "velocity_limit": "10",             # HIGH_VELOCITY fires above this count
    "velocity_window_minutes": "60",
    "velocity_points": "30",
    "home_country": "ZA",               # CROSS_BORDER fires for any other country
    "cross_border_points": "20",
    "medium_risk_score": "40",          # score >= 40 is MEDIUM
    "high_risk_score": "70",            # score >= 70 is HIGH and sends an alert
    "metrics_namespace": "FinTrust/ETL",
    "db_port": "5432",
    "db_name": "fintrust",
    "db_auth_mode": "iam",              # "iam" in AWS, "password" for local tests
}


class ConfigError(Exception):
    """A required setting is missing or has an invalid value."""


def region():
    """AWS region. Lambda always sets AWS_REGION for us."""
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )


def environment():
    """Deployment environment name, e.g. dev."""
    return os.environ.get("FINTRUST_ENV", "dev")


def ssm_prefix():
    return f"/fintrust/{environment()}/"


def ssm_enabled():
    return os.environ.get("FINTRUST_SSM_ENABLED", "true").strip().lower() != "false"


# Created ONCE at module level, outside any Lambda handler. Lambda keeps the
# module loaded between invocations ("warm start"), so the client and its
# network connection are reused instead of being rebuilt on every call.
# Short timeouts mean a network problem fails in seconds, not minutes.
_ssm = boto3.client(
    "ssm",
    region_name=region(),
    config=BotoConfig(
        connect_timeout=2,
        read_timeout=3,
        retries={"max_attempts": 2, "mode": "standard"},
    ),
)

# Filled on the first lookup, then reused for the life of the Lambda container.
_ssm_cache = {}
_ssm_loaded = False


def _load_from_ssm():
    """Fetch every parameter under /fintrust/<env>/ in one paginated call."""
    global _ssm_loaded
    prefix = ssm_prefix()
    try:
        paginator = _ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
            for parameter in page["Parameters"]:
                name = parameter["Name"][len(prefix):]
                _ssm_cache[name] = parameter["Value"]
    except (ClientError, BotoCoreError) as error:
        # Log the reason, never the values.
        logger.error("ssm_load_failed", prefix=prefix, error=str(error))
        raise ConfigError(f"Could not load configuration from SSM path {prefix}") from error
    _ssm_loaded = True
    logger.info("ssm_loaded", prefix=prefix, parameter_count=len(_ssm_cache))


def get(name, default=None):
    """Return a setting as text.

    `name` is lower-case with underscores, e.g. "raw_bucket".
    `default` is used only if the setting is in neither source nor DEFAULTS.
    """
    env_value = os.environ.get(f"FINTRUST_{name.upper()}")
    if env_value is not None and env_value.strip() != "":
        return env_value.strip()

    if ssm_enabled():
        if not _ssm_loaded:
            _load_from_ssm()
        if name in _ssm_cache:
            return _ssm_cache[name]

    if name in DEFAULTS:
        return DEFAULTS[name]
    if default is not None:
        return default

    raise ConfigError(
        f"Missing required setting '{name}'. Set FINTRUST_{name.upper()} "
        f"or create SSM parameter {ssm_prefix()}{name}"
    )


def get_int(name, default=None):
    """Return a setting as a whole number."""
    value = get(name, None if default is None else str(default))
    try:
        return int(value)
    except ValueError as error:
        raise ConfigError(f"Setting '{name}' must be a whole number, got '{value}'") from error


def get_decimal(name, default=None):
    """Return a setting as a Decimal.

    Money is always Decimal, never float: 0.1 + 0.2 is not exactly 0.3 in
    floating point, and a bank ledger cannot have rounding drift.
    """
    value = get(name, None if default is None else str(default))
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ConfigError(f"Setting '{name}' must be a number, got '{value}'") from error


def reset_cache():
    """Forget values loaded from SSM. Used by tests."""
    global _ssm_loaded
    _ssm_cache.clear()
    _ssm_loaded = False
