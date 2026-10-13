"""Checks on the sign-in infrastructure scripts (Week 15 Day 2).

The scripts run against a real AWS account, so these tests read them and fail
the build if a security setting is weakened by mistake: MFA off, self sign-up
on, the implicit flow, a client secret in the browser, a route left without
sign-in, the sessions table outside the Free Tier.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COGNITO = ROOT / "infrastructure" / "cognito"
AUTH_SCRIPT = (COGNITO / "deploy_auth.sh").read_text()
API_SCRIPT = (ROOT / "infrastructure" / "apigateway" / "deploy_api.sh").read_text()
TABLE_SCRIPT = (ROOT / "infrastructure" / "dynamodb" / "deploy_table.sh").read_text()


def settings_block(name):
    """The bash array of app client settings, e.g. DASHBOARD_SETTINGS=( ... )."""
    return re.search(rf"{name}=\((.*?)\n\)", AUTH_SCRIPT, re.S).group(1)


class TestUserPool:
    def test_mfa_is_required_by_authenticator_app(self):
        assert ("--software-token-mfa-configuration Enabled=true --mfa-configuration ON" in AUTH_SCRIPT)
        # SMS costs money and is the weaker factor: no SMS settings anywhere.
        assert "sms-mfa-configuration" not in AUTH_SCRIPT and "SmsConfiguration" not in AUTH_SCRIPT

    def test_only_an_administrator_can_create_accounts(self):
        invitation = json.loads((COGNITO / "invitation.json").read_text())
        assert invitation["AllowAdminCreateUserOnly"] is True
        message = invitation["InviteMessageTemplate"]["EmailMessage"]
        assert "{username}" in message and "{####}" in message           # Cognito requires both placeholders

    def test_password_policy_is_strong(self):
        policy = json.loads((COGNITO / "password-policy.json").read_text())["PasswordPolicy"]
        assert policy["MinimumLength"] >= 12
        assert all(policy[rule] for rule in ("RequireUppercase", "RequireLowercase", "RequireNumbers", "RequireSymbols"))
        assert policy["TemporaryPasswordValidityDays"] <= 7 and policy["PasswordHistorySize"] >= 5

    def test_free_tier_plan_and_deletion_protection(self):
        assert "--user-pool-tier ESSENTIALS" in AUTH_SCRIPT                 # 10 000 monthly active users free
        assert "--deletion-protection ACTIVE" in AUTH_SCRIPT
        assert "PLUS" not in AUTH_SCRIPT                                     # the Plus plan has no free tier

    def test_the_two_roles_are_created(self):
        assert re.findall(r"^make_group ([a-z-]+) (\d)", AUTH_SCRIPT, re.M) == [
            ("compliance-officer", "1"), ("compliance-analyst", "2")]

    def test_managed_login_hosts_the_password_pages(self):
        assert "--managed-login-version 2" in AUTH_SCRIPT
        assert "create-managed-login-branding" in AUTH_SCRIPT


class TestDashboardClient:
    settings = settings_block("DASHBOARD_SETTINGS")

    def test_has_no_secret(self):
        assert '--client-name "$DASHBOARD_CLIENT_NAME" --no-generate-secret' in AUTH_SCRIPT

    def test_code_flow_only_never_implicit(self):
        assert "--allowed-o-auth-flows code " in self.settings and "implicit" not in AUTH_SCRIPT

    def test_returns_only_to_the_dashboard_address(self):
        assert '--callback-urls "$RETURN_URL" --logout-urls "$RETURN_URL"' in self.settings
        assert "localhost" not in AUTH_SCRIPT

    def test_short_tokens_and_revocation(self):
        assert "--id-token-validity 30" in self.settings and "--refresh-token-validity 8" in self.settings
        assert "IdToken=minutes" in self.settings and "RefreshToken=hours" in self.settings
        assert "--enable-token-revocation" in self.settings
        assert "--prevent-user-existence-errors ENABLED" in self.settings

    def test_cannot_sign_in_with_a_plain_password_from_a_browser(self):
        assert "ALLOW_USER_PASSWORD_AUTH" not in self.settings and "ALLOW_ADMIN_USER_PASSWORD_AUTH" not in self.settings


class TestAutomationClient:
    settings = settings_block("AUTOMATION_SETTINGS")

    def test_signs_in_only_with_administrator_credentials(self):
        assert "--explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH" in self.settings
        assert "allowed-o-auth" not in self.settings                         # never from a browser


class TestApiGateway:
    def test_refuses_to_publish_without_sign_in(self):
        assert 'echo "ERROR: sign-in is not set up. Run ./infrastructure/cognito/deploy_auth.sh first."' in API_SCRIPT

    def test_every_route_but_health_is_forced_to_require_a_sign_in(self):
        assert 'if [[ "$RESOURCE_PATH" == "/health" ]]' in API_SCRIPT
        assert "op=replace,path=/authorizationType,value=COGNITO_USER_POOLS" in API_SCRIPT
        assert '"op=replace,path=/authorizerId,value=${AUTHORIZER_ID}"' in API_SCRIPT

    def test_dashboard_and_scripts_have_separate_api_keys(self):
        keys = re.findall(r'^make_key "\$\{PREFIX\}-([a-z]+)-key"', API_SCRIPT, re.M)
        assert keys == ["compliance", "dashboard"]


class TestSessionsTable:
    def test_is_inside_the_free_tier_with_the_events_table(self):
        """25 read and 25 write units are free across all tables and indexes."""
        events = re.search(r"--provisioned-throughput ReadCapacityUnits=(\d+),WriteCapacityUnits=(\d+)", TABLE_SCRIPT)
        index = re.search(r'"ProvisionedThroughput": \{"ReadCapacityUnits": (\d+), "WriteCapacityUnits": (\d+)\}', TABLE_SCRIPT)
        session_block = TABLE_SCRIPT[TABLE_SCRIPT.index('--table-name "$SESSIONS_TABLE"'):]
        sessions = re.search(r"ReadCapacityUnits=(\d+),WriteCapacityUnits=(\d+)", session_block)
        reads = sum(int(found.group(1)) for found in (events, index, sessions))
        writes = sum(int(found.group(2)) for found in (events, index, sessions))
        assert reads <= 25 and writes <= 25

    def test_old_sign_in_records_delete_themselves(self):
        assert '--time-to-live-specification "Enabled=true,AttributeName=expires_at"' in \
            TABLE_SCRIPT[TABLE_SCRIPT.index("the sign-in session table"):]

    def test_the_table_name_is_saved_for_the_api_lambda(self):
        assert 'put_param sessions_table "$SESSIONS_TABLE"' in TABLE_SCRIPT


class TestLambdaEnvironment:
    @pytest.fixture
    def build_env(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("build_env", ROOT / "infrastructure" / "lambda" / "build_env.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_api_lambda_cannot_start_without_the_sessions_table(self, build_env):
        assert "FINTRUST_SESSIONS_TABLE" in build_env.REQUIRED["query"]

    def test_user_pool_details_are_kept_out_of_every_lambda(self, build_env):
        assert all("cognito_" in skipped for skipped in build_env.NOT_NEEDED.values())
