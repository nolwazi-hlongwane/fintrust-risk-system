"""Tests for the sign-in tools in scripts/ (Week 15 Day 2).

    totp.py           the 6-digit authenticator codes, checked against RFC 6238's own test values
    api_auth.py       how the verification scripts sign in and what they send
    manage_users.py   creating people, changing roles, ending sessions, the automation user

Cognito is faked with moto where moto supports it. moto does not ask for MFA,
so the MFA steps are checked against a recorded stand-in for the Cognito client.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest

from tests.conftest import REGION

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import api_auth  # noqa: E402
import manage_users  # noqa: E402
import totp  # noqa: E402

# RFC 6238, Appendix B: the secret "12345678901234567890" (ASCII), SHA-1, 8 digits.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class TestTotp:
    @pytest.mark.parametrize("seconds, expected", [
        (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
        (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130"),
    ])
    def test_matches_the_rfc_6238_test_values(self, seconds, expected):
        assert totp.code_at(RFC_SECRET, totp.current_step(seconds), digits=8) == expected

    def test_six_digits_is_the_last_six_of_the_same_number(self):
        assert totp.code_at(RFC_SECRET, totp.current_step(59)) == "287082"

    def test_secrets_are_read_without_padding_spaces_or_case(self):
        assert totp.code_at(RFC_SECRET.lower()[:16] + " " + RFC_SECRET[16:], 1) == totp.code_at(RFC_SECRET, 1)

    def test_a_used_code_is_never_given_twice(self):
        slept = []
        code, step = totp.fresh_code(RFC_SECRET, used_step=totp.current_step(100), now=100, sleep=slept.append)
        assert step == totp.current_step(100) + 1 and slept and 0 < slept[0] <= 31
        assert code == totp.code_at(RFC_SECRET, step)

    def test_a_fresh_step_needs_no_wait(self):
        slept = []
        _code, step = totp.fresh_code(RFC_SECRET, used_step=None, now=100, sleep=slept.append)
        assert step == 3 and slept == []


class TestCredentials:
    def test_headers_carry_the_key_and_the_token(self):
        credentials = api_auth.Credentials("key-value", "token-value", "fintrust-automation")
        assert credentials.headers() == {"x-api-key": "key-value", "Authorization": "token-value"}

    def test_a_bare_key_and_nothing_are_still_accepted(self):
        assert api_auth.headers_for("key-value") == {"x-api-key": "key-value"}
        assert api_auth.headers_for(None) == {}

    def test_secrets_never_appear_when_printed(self):
        text = repr(api_auth.Credentials("key-value-123", "eyJsecret", "fintrust-automation"))
        assert "key-value-123" not in text and "eyJ" not in text


class TestIdToken:
    @pytest.fixture
    def session(self, aws):
        ssm = boto3.client("ssm", region_name=REGION)
        for name, value in (("cognito_user_pool_id", "eu-north-1_pool"), ("cognito_automation_client_id", "client")):
            ssm.put_parameter(Name=f"/fintrust/{api_auth.ENVIRONMENT}/{name}", Value=value, Type="String")
        ssm.put_parameter(Name=api_auth.PASSWORD_PARAMETER, Value="Pa55word!long", Type="SecureString")
        ssm.put_parameter(Name=api_auth.TOTP_PARAMETER, Value=RFC_SECRET, Type="SecureString")
        cognito = MagicMock()
        cognito.admin_initiate_auth.return_value = {"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "s1"}
        cognito.admin_respond_to_auth_challenge.return_value = {"AuthenticationResult": {"IdToken": "eyJ.id.token"}}
        real = boto3.Session(region_name=REGION)
        fake_session = MagicMock()
        fake_session.client.side_effect = lambda name: cognito if name == "cognito-idp" else real.client(name)
        fake_session.cognito = cognito
        return fake_session

    def test_signs_in_with_the_password_then_the_current_code(self, session, monkeypatch):
        monkeypatch.setattr(api_auth, "fresh_code", lambda secret, used=None: ("123456", 42))
        token, step = api_auth.id_token(session)
        assert (token, step) == ("eyJ.id.token", 42)
        started = session.cognito.admin_initiate_auth.call_args.kwargs
        assert started["AuthFlow"] == "ADMIN_USER_PASSWORD_AUTH"
        assert started["AuthParameters"] == {"USERNAME": "fintrust-automation", "PASSWORD": "Pa55word!long"}
        answered = session.cognito.admin_respond_to_auth_challenge.call_args.kwargs
        assert answered["ChallengeResponses"]["SOFTWARE_TOKEN_MFA_CODE"] == "123456"

    def test_a_code_already_used_is_retried_with_the_next_one(self, session, monkeypatch):
        from botocore.exceptions import ClientError

        codes = iter([("111111", 1), ("222222", 2)])
        monkeypatch.setattr(api_auth, "fresh_code", lambda secret, used=None: next(codes))
        session.cognito.admin_respond_to_auth_challenge.side_effect = [
            ClientError({"Error": {"Code": "CodeMismatchException", "Message": "Invalid code"}}, "Respond"),
            {"AuthenticationResult": {"IdToken": "eyJ.second"}},
        ]
        assert api_auth.id_token(session) == ("eyJ.second", 2)

    def test_without_sign_in_set_up_the_message_says_what_to_run(self, aws):
        with pytest.raises(api_auth.SignInFailed, match="deploy_auth.sh"):
            api_auth.id_token(boto3.Session(region_name=REGION))


@pytest.fixture
def pool(aws, sessions_table, monkeypatch):
    """A moto user pool with the two role groups, and its settings in SSM."""
    cognito = boto3.client("cognito-idp", region_name=REGION)
    pool_id = cognito.create_user_pool(PoolName="fintrust-capstone-users")["UserPool"]["Id"]
    for group in manage_users.ROLES:
        cognito.create_group(UserPoolId=pool_id, GroupName=group)
    client_id = cognito.create_user_pool_client(UserPoolId=pool_id, ClientName="fintrust-capstone-automation",
                                                ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH"])["UserPoolClient"]["ClientId"]
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name=f"/fintrust/{manage_users.ENVIRONMENT}/cognito_user_pool_id", Value=pool_id, Type="String")
    ssm.put_parameter(Name=f"/fintrust/{manage_users.ENVIRONMENT}/cognito_automation_client_id", Value=client_id, Type="String")
    monkeypatch.setattr(manage_users, "REGION", REGION)
    return cognito, pool_id


def groups_of(cognito, pool_id, username):
    return [group["GroupName"] for group in cognito.admin_list_groups_for_user(UserPoolId=pool_id, Username=username)["Groups"]]


@pytest.mark.real_sessions
class TestManageUsers:
    def test_create_gives_exactly_one_role(self, pool):
        cognito, pool_id = pool
        manage_users.main(["create", "--username", "t.nkosi", "--name", "Thabo Nkosi",
                           "--email", "thabo.nkosi@fintrust.example", "--role", "compliance-analyst"])
        user = cognito.admin_get_user(UserPoolId=pool_id, Username="t.nkosi")
        attributes = {item["Name"]: item["Value"] for item in user["UserAttributes"]}
        assert attributes["name"] == "Thabo Nkosi" and attributes["email"] == "thabo.nkosi@fintrust.example"
        assert groups_of(cognito, pool_id, "t.nkosi") == ["compliance-analyst"]

    @pytest.mark.parametrize("username", ["T.Nkosi", "x", "bad name", "a" * 61])
    def test_unsafe_usernames_are_refused(self, pool, username):
        with pytest.raises(SystemExit):
            manage_users.main(["create", "--username", username, "--name", "X", "--email", "x@fintrust.example",
                               "--role", "compliance-analyst"])

    def test_changing_role_moves_the_person_and_signs_them_out(self, pool):
        from fintrust import sessions

        cognito, pool_id = pool
        manage_users.main(["create", "--username", "t.nkosi", "--name", "Thabo Nkosi",
                           "--email", "thabo.nkosi@fintrust.example", "--role", "compliance-analyst"])
        sessions.start("laptop", "t.nkosi", ("compliance-analyst",))
        manage_users.main(["set-role", "--username", "t.nkosi", "--role", "compliance-officer"])
        assert groups_of(cognito, pool_id, "t.nkosi") == ["compliance-officer"]
        with pytest.raises(sessions.SessionEnded) as ended:
            sessions.require_active("laptop", "t.nkosi")
        assert ended.value.reason == "REVOKED"

    def test_disable_stops_sign_in_but_keeps_the_account(self, pool):
        cognito, pool_id = pool
        manage_users.main(["create", "--username", "t.nkosi", "--name", "Thabo Nkosi",
                           "--email", "thabo.nkosi@fintrust.example", "--role", "compliance-officer"])
        manage_users.main(["disable", "--username", "t.nkosi"])
        assert cognito.admin_get_user(UserPoolId=pool_id, Username="t.nkosi")["Enabled"] is False


class TestAutomationUser:
    """The MFA set-up steps, against a recorded stand-in (moto does not ask for MFA)."""

    def test_is_rebuilt_with_mfa_and_its_secrets_are_stored_encrypted(self, aws, monkeypatch):
        ssm = boto3.client("ssm", region_name=REGION)
        ssm.put_parameter(Name=f"/fintrust/{manage_users.ENVIRONMENT}/cognito_user_pool_id", Value="eu-north-1_pool", Type="String")
        ssm.put_parameter(Name=f"/fintrust/{manage_users.ENVIRONMENT}/cognito_automation_client_id", Value="client", Type="String")
        cognito = MagicMock()
        cognito.exceptions.UserNotFoundException = type("UserNotFound", (Exception,), {})
        cognito.admin_initiate_auth.return_value = {"ChallengeName": "MFA_SETUP", "Session": "s1"}
        cognito.associate_software_token.return_value = {"SecretCode": RFC_SECRET, "Session": "s2"}
        cognito.verify_software_token.return_value = {"Status": "SUCCESS", "Session": "s3"}
        monkeypatch.setattr(manage_users, "clients", lambda: (cognito, ssm))

        manage_users.main(["automation"])

        assert cognito.admin_delete_user.call_args.kwargs["Username"] == "fintrust-automation"
        created = cognito.admin_create_user.call_args.kwargs
        assert created["MessageAction"] == "SUPPRESS"                       # no email for a robot
        password = cognito.admin_set_user_password.call_args.kwargs["Password"]
        assert len(password) == 32 and cognito.admin_set_user_password.call_args.kwargs["Permanent"] is True
        assert cognito.admin_add_user_to_group.call_args.kwargs["GroupName"] == "compliance-officer"
        code = cognito.verify_software_token.call_args.kwargs["UserCode"]
        assert len(code) == 6 and code.isdigit()
        assert cognito.admin_respond_to_auth_challenge.call_args.kwargs["ChallengeName"] == "MFA_SETUP"
        stored = {name: ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
                  for name in (api_auth.PASSWORD_PARAMETER, api_auth.TOTP_PARAMETER)}
        assert stored[api_auth.PASSWORD_PARAMETER]["Type"] == "SecureString"
        assert stored[api_auth.PASSWORD_PARAMETER]["Value"] == password
        assert stored[api_auth.TOTP_PARAMETER]["Value"] == RFC_SECRET

    def test_generated_passwords_meet_the_pool_policy(self):
        for _ in range(50):
            password = manage_users._new_password()
            assert len(password) >= 12 and any(c.isupper() for c in password) and any(c.islower() for c in password)
            assert any(c.isdigit() for c in password) and any(c in "!#%+-=?@^_" for c in password)
