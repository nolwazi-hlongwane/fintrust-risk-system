"""Unit tests for fintrust.config."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from fintrust import config
from fintrust.config import ConfigError


class TestEnvironmentVariables:
    def test_environment_variable_is_used(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_RAW_BUCKET", "bucket-from-env")
        assert config.get("raw_bucket") == "bucket-from-env"

    def test_environment_variable_wins_over_ssm(self, monkeypatch, put_parameter):
        put_parameter("raw_bucket", "bucket-from-ssm")
        monkeypatch.setenv("FINTRUST_RAW_BUCKET", "bucket-from-env")
        assert config.get("raw_bucket") == "bucket-from-env"

    def test_blank_environment_variable_is_ignored(self, monkeypatch, put_parameter):
        put_parameter("raw_bucket", "bucket-from-ssm")
        monkeypatch.setenv("FINTRUST_RAW_BUCKET", "   ")
        assert config.get("raw_bucket") == "bucket-from-ssm"

    def test_value_is_stripped(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_RAW_BUCKET", "  my-bucket \n")
        assert config.get("raw_bucket") == "my-bucket"


class TestSsmParameterStore:
    def test_value_is_read_from_ssm(self, put_parameter):
        put_parameter("queue_url", "https://sqs.example/queue.fifo")
        assert config.get("queue_url") == "https://sqs.example/queue.fifo"

    def test_secure_string_is_decrypted(self, put_parameter):
        put_parameter("some_secret", "s3cret", secure=True)
        assert config.get("some_secret") == "s3cret"

    def test_ssm_overrides_built_in_default(self, put_parameter):
        put_parameter("high_risk_score", "80")
        assert config.get_int("high_risk_score") == 80

    def test_more_than_one_page_of_parameters(self, put_parameter):
        # SSM returns at most 10 parameters per page, so 25 forces pagination.
        for number in range(25):
            put_parameter(f"setting_{number}", str(number))
        assert config.get("setting_0") == "0"
        assert config.get("setting_24") == "24"

    def test_ssm_is_called_only_once(self, put_parameter):
        put_parameter("raw_bucket", "a")
        put_parameter("queue_url", "b")
        with patch.object(config, "_load_from_ssm", wraps=config._load_from_ssm) as load:
            config.get("raw_bucket")
            config.get("queue_url")
            config.get("raw_bucket")
        assert load.call_count == 1

    def test_only_this_environments_parameters_are_loaded(self, ssm, put_parameter):
        ssm.put_parameter(Name="/fintrust/prod/raw_bucket", Value="prod-bucket", Type="String")
        put_parameter("raw_bucket", "test-bucket")
        assert config.get("raw_bucket") == "test-bucket"

    def test_ssm_failure_raises_config_error(self, aws):
        denied = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetParametersByPath",
        )
        with patch.object(config._ssm, "get_paginator", side_effect=denied):
            with pytest.raises(ConfigError, match="Could not load configuration"):
                config.get("raw_bucket")

    def test_ssm_failure_is_logged_without_values(self, aws, capsys):
        denied = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetParametersByPath",
        )
        with patch.object(config._ssm, "get_paginator", side_effect=denied):
            with pytest.raises(ConfigError):
                config.get("raw_bucket")
        assert "ssm_load_failed" in capsys.readouterr().out

    def test_ssm_not_called_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        with patch.object(config, "_load_from_ssm") as load:
            assert config.get("home_country") == "ZA"
        load.assert_not_called()


class TestDefaultsAndMissing:
    def test_built_in_default(self, aws):
        assert config.get("home_country") == "ZA"
        assert config.get("metrics_namespace") == "FinTrust/ETL"

    def test_caller_default(self, aws):
        assert config.get("page_size", "50") == "50"

    def test_missing_required_setting_raises(self, aws):
        with pytest.raises(ConfigError) as error:
            config.get("raw_bucket")
        message = str(error.value)
        assert "FINTRUST_RAW_BUCKET" in message
        assert "/fintrust/test/raw_bucket" in message

    def test_risk_rule_defaults_match_the_agreed_model(self, aws):
        assert config.get_decimal("high_amount_threshold") == Decimal("50000")
        assert config.get_int("high_amount_points") == 50
        assert config.get_int("velocity_limit") == 10
        assert config.get_int("velocity_window_minutes") == 60
        assert config.get_int("velocity_points") == 30
        assert config.get_int("cross_border_points") == 20
        assert config.get_int("medium_risk_score") == 40
        assert config.get_int("high_risk_score") == 70

    def test_rule_points_can_reach_high_threshold(self, aws):
        total = (
            config.get_int("high_amount_points")
            + config.get_int("velocity_points")
            + config.get_int("cross_border_points")
        )
        assert total == 100
        assert total >= config.get_int("high_risk_score")


class TestTypedGetters:
    def test_get_int(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_DB_PORT", "5433")
        assert config.get_int("db_port") == 5433

    def test_get_int_rejects_text(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_VELOCITY_LIMIT", "ten")
        with pytest.raises(ConfigError, match="whole number"):
            config.get_int("velocity_limit")

    def test_get_int_with_default(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        assert config.get_int("page_size", 50) == 50

    def test_get_decimal_is_exact(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_HIGH_AMOUNT_THRESHOLD", "50000.10")
        value = config.get_decimal("high_amount_threshold")
        assert isinstance(value, Decimal)
        assert value == Decimal("50000.10")

    def test_get_decimal_rejects_text(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_HIGH_AMOUNT_THRESHOLD", "R50k")
        with pytest.raises(ConfigError, match="must be a number"):
            config.get_decimal("high_amount_threshold")


class TestHelpers:
    def test_region_comes_from_lambda_variable(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "af-south-1")
        assert config.region() == "af-south-1"

    def test_region_falls_back_to_stockholm(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert config.region() == "eu-north-1"

    def test_ssm_prefix_uses_environment(self, monkeypatch):
        monkeypatch.setenv("FINTRUST_ENV", "prod")
        assert config.ssm_prefix() == "/fintrust/prod/"

    def test_ssm_client_is_created_at_module_level(self):
        assert config._ssm.meta.service_model.service_name == "ssm"
