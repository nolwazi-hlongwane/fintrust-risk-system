"""Manage who can sign in to the FinTrust risk desk (Week 15 Day 2).

There is no self sign-up: a bank decides who gets access. An administrator
(anyone with IAM rights on the user pool) uses this script.

    python scripts/manage_users.py create --username t.nkosi --name "Thabo Nkosi" \\
        --email thabo.nkosi@fintrust.example --role compliance-officer
        Cognito emails the person a temporary password. At first sign-in they
        choose their own password and set up an authenticator app.

    python scripts/manage_users.py list
    python scripts/manage_users.py set-role --username t.nkosi --role compliance-analyst
    python scripts/manage_users.py end-sessions --username t.nkosi
        Signs the person out everywhere at once: a lost laptop, a suspected
        compromise. Revokes their refresh tokens in Cognito and ends their API
        sessions in DynamoDB, so even an unexpired ID token stops working.
    python scripts/manage_users.py disable --username t.nkosi
        Someone leaves: they cannot sign in again, and every session ends now.
        The account is kept (disabled, not deleted) so the audit trail of who
        resolved what still points at a real account.

    python scripts/manage_users.py automation
        Creates or repairs the automation user that the verification scripts
        and the end-to-end test sign in as. Run by deploy_auth.sh.

Roles are Cognito groups; the API reads them from the ID token:
    compliance-officer   read, upload, resolve alerts, flag accounts
    compliance-analyst   read and upload
"""

import argparse
import os
import re
import secrets
import string
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the fintrust package
sys.path.insert(0, str(Path(__file__).resolve().parent))         # api_auth, totp

from api_auth import AUTOMATION_USERNAME, PASSWORD_PARAMETER, TOTP_PARAMETER  # noqa: E402
from totp import fresh_code  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-north-1")
ENVIRONMENT = os.environ.get("FINTRUST_ENV", "dev")
ROLES = ("compliance-officer", "compliance-analyst")
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,59}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clients():
    session = boto3.Session(region_name=REGION)
    return session.client("cognito-idp"), session.client("ssm")


def setting(ssm, name):
    try:
        return ssm.get_parameter(Name=f"/fintrust/{ENVIRONMENT}/{name}")["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        sys.exit(f"ERROR: /fintrust/{ENVIRONMENT}/{name} is not set. "
                 "Run ./infrastructure/cognito/deploy_auth.sh first.")


def check_username(username):
    if not USERNAME.match(username):
        sys.exit("ERROR: a username is 2 to 60 lower-case letters, digits, dots, dashes or underscores, "
                 "for example t.nkosi")
    return username


def set_groups(cognito, pool_id, username, role):
    """Put the person in exactly one role group."""
    for group in ROLES:
        if group != role:
            cognito.admin_remove_user_from_group(UserPoolId=pool_id, Username=username, GroupName=group)
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=role)


def end_api_sessions(username):
    """End the person's sessions in the DynamoDB session table."""
    os.environ.setdefault("FINTRUST_ENV", ENVIRONMENT)
    from fintrust import sessions
    return sessions.end_all_for_user(username, sessions.REVOKED)


# --------------------------------------------------------------------- commands

def create(arguments):
    cognito, ssm = clients()
    pool_id = setting(ssm, "cognito_user_pool_id")
    username = check_username(arguments.username)
    if not EMAIL.match(arguments.email):
        sys.exit("ERROR: that does not look like an email address")
    cognito.admin_create_user(
        UserPoolId=pool_id, Username=username, DesiredDeliveryMediums=["EMAIL"],
        UserAttributes=[{"Name": "email", "Value": arguments.email}, {"Name": "email_verified", "Value": "true"},
                        {"Name": "name", "Value": arguments.name.strip()[:120]}],
    )
    set_groups(cognito, pool_id, username, arguments.role)
    print(f"Created {username} ({arguments.role}). An invitation with a temporary password has been emailed.")
    print("At first sign-in they choose a password and set up an authenticator app.")


def list_users(_arguments):
    cognito, ssm = clients()
    pool_id = setting(ssm, "cognito_user_pool_id")
    members = {}
    for role in ROLES:
        for page in cognito.get_paginator("list_users_in_group").paginate(UserPoolId=pool_id, GroupName=role):
            for user in page["Users"]:
                members.setdefault(user["Username"], []).append(role)
    print(f"{'Username':<24} {'Role':<22} {'Status':<22} Enabled")
    for page in cognito.get_paginator("list_users").paginate(UserPoolId=pool_id):
        for user in page["Users"]:
            name = user["Username"]
            print(f"{name:<24} {', '.join(members.get(name, ['(no role)'])):<22} {user['UserStatus']:<22} "
                  f"{'yes' if user['Enabled'] else 'no'}")


def set_role(arguments):
    cognito, ssm = clients()
    pool_id = setting(ssm, "cognito_user_pool_id")
    username = check_username(arguments.username)
    set_groups(cognito, pool_id, username, arguments.role)
    # A role is read from the ID token, so it takes effect at the next sign-in.
    cognito.admin_user_global_sign_out(UserPoolId=pool_id, Username=username)
    ended = end_api_sessions(username)
    print(f"{username} is now {arguments.role}. {ended} session(s) ended; the new role applies at the next sign-in.")


def end_sessions(arguments):
    cognito, ssm = clients()
    pool_id = setting(ssm, "cognito_user_pool_id")
    username = check_username(arguments.username)
    cognito.admin_user_global_sign_out(UserPoolId=pool_id, Username=username)
    ended = end_api_sessions(username)
    print(f"Signed {username} out everywhere: refresh tokens revoked, {ended} API session(s) ended.")


def disable(arguments):
    cognito, ssm = clients()
    pool_id = setting(ssm, "cognito_user_pool_id")
    username = check_username(arguments.username)
    cognito.admin_disable_user(UserPoolId=pool_id, Username=username)
    cognito.admin_user_global_sign_out(UserPoolId=pool_id, Username=username)
    ended = end_api_sessions(username)
    print(f"{username} can no longer sign in. {ended} API session(s) ended. The account is kept for the audit trail.")


def _new_password():
    """A random password that meets the pool's policy (upper, lower, digit, symbol, 12+)."""
    alphabet = string.ascii_letters + string.digits + "!#%+-=?@^_"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(32))
        if (any(c.islower() for c in password) and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password) and any(c in "!#%+-=?@^_" for c in password)):
            return password


def automation(_arguments):
    """Create the automation user from scratch: password, officer role and MFA.

    Rebuilt on every run, so its secrets are rotated each time sign-in is
    deployed and a lost secret is never a problem.
    """
    cognito, ssm = clients()
    pool_id = setting(ssm, "cognito_user_pool_id")
    client_id = setting(ssm, "cognito_automation_client_id")
    try:
        cognito.admin_delete_user(UserPoolId=pool_id, Username=AUTOMATION_USERNAME)
    except cognito.exceptions.UserNotFoundException:
        pass

    password = _new_password()
    cognito.admin_create_user(
        UserPoolId=pool_id, Username=AUTOMATION_USERNAME, MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "name", "Value": "Automated verification"}],
    )
    cognito.admin_set_user_password(UserPoolId=pool_id, Username=AUTOMATION_USERNAME, Password=password,
                                    Permanent=True)
    set_groups(cognito, pool_id, AUTOMATION_USERNAME, "compliance-officer")

    # First sign-in: Cognito asks for MFA to be set up, exactly as it does for a person.
    started = cognito.admin_initiate_auth(
        UserPoolId=pool_id, ClientId=client_id, AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": AUTOMATION_USERNAME, "PASSWORD": password},
    )
    if started.get("ChallengeName") != "MFA_SETUP":
        sys.exit(f"ERROR: expected the MFA set-up step, got {started.get('ChallengeName')}. "
                 "Is MFA required on the user pool?")
    associated = cognito.associate_software_token(Session=started["Session"])
    secret = associated["SecretCode"]
    code, step = fresh_code(secret)
    verified = cognito.verify_software_token(Session=associated["Session"], UserCode=code,
                                             FriendlyDeviceName="verification-scripts")
    if verified["Status"] != "SUCCESS":
        sys.exit("ERROR: the authenticator code was not accepted")
    cognito.admin_respond_to_auth_challenge(
        UserPoolId=pool_id, ClientId=client_id, ChallengeName="MFA_SETUP", Session=verified["Session"],
        ChallengeResponses={"USERNAME": AUTOMATION_USERNAME},
    )

    for name, value in ((PASSWORD_PARAMETER, password), (TOTP_PARAMETER, secret)):
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
    print(f"Automation user {AUTOMATION_USERNAME} is ready (compliance-officer, MFA on).")
    print(f"Its password and MFA secret are encrypted in SSM under /fintrust-admin/{ENVIRONMENT}/, never printed.")
    return step


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage who can sign in to the FinTrust risk desk.")
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("create", help="create a person's account and email them an invitation")
    command.add_argument("--username", required=True)
    command.add_argument("--name", required=True, help="full name, shown in the dashboard")
    command.add_argument("--email", required=True)
    command.add_argument("--role", required=True, choices=ROLES)
    command.set_defaults(run=create)

    commands.add_parser("list", help="everyone who can sign in, with their role").set_defaults(run=list_users)

    command = commands.add_parser("set-role", help="change a person's role")
    command.add_argument("--username", required=True)
    command.add_argument("--role", required=True, choices=ROLES)
    command.set_defaults(run=set_role)

    for name, run, text in (("end-sessions", end_sessions, "sign a person out everywhere, now"),
                            ("disable", disable, "stop a person signing in, and end every session")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--username", required=True)
        command.set_defaults(run=run)

    commands.add_parser("automation", help="create or repair the automation user").set_defaults(run=automation)

    arguments = parser.parse_args(argv)
    arguments.run(arguments)
    return 0


if __name__ == "__main__":
    sys.exit(main())
