# Security Plan and IAM Role Matrix

**Brief slot:** Week 13 Day 1: Design Kickoff and Infrastructure Setup · **Status:** roles and boundary deployed on Day 1 with base permissions; data permissions are added on the day each resource is created (see the Status column). Sign-in for people, with roles and sessions, added in Week 15 Day 2.

## Principles

1. **Least privilege.** One execution role per Lambda. Each role lists exact actions on exact resources. No wildcard actions anywhere; this is enforced by an automated test (`tests/unit/test_iam_policies.py`) that fails the build if one is added.
2. **Defence in depth.** A request must pass several independent controls: API key, a signed-in person (Cognito token with MFA), an open session, the person's role, IAM role, permission boundary, security group, database user rights, encryption.
3. **No long-lived secrets.** No AWS keys in code. No database password in any Lambda: the database accepts short-lived IAM tokens.
4. **Private by default.** The database has no public endpoint. Buckets block all public access.

## IAM role matrix

One row per Lambda function. "Base" permissions are deployed now; the rest are the design that later phases implement.

| Lambda (role) | Permitted actions | On which resources | Status |
|---|---|---|---|
| **ingest** (`fintrust-capstone-ingest-role`) | `logs:CreateLogStream`, `logs:PutLogEvents` | Its own log group only | Deployed W13 D1 |
| | `xray:PutTraceSegments`, `xray:PutTelemetryRecords` | `*` (AWS does not support a narrower resource) | Deployed W13 D1 |
| | `ssm:GetParametersByPath` | `/fintrust/dev/*` | Deployed W13 D1 |
| | `s3:GetObject` | Raw bucket objects | Deployed W13 D3 |
| | `s3:PutObject` | Processed and quarantine bucket objects | Deployed W13 D3 |
| | `sqs:SendMessage` | Transactions FIFO queue; ingest DLQ | Deployed W13 D3 |
| | `dynamodb:PutItem` | Events table | Deployed W13 D3 |
| | `kms:Decrypt`, `kms:GenerateDataKey` | The project key | Deployed W13 D3 |
| **score** (`fintrust-capstone-score-role`) | Logs, X-Ray (as above) | Its own log group | Deployed W13 D1 |
| | Six `ec2:` network interface actions | `*` (required for any Lambda in a VPC) | Deployed W13 D1 |
| | `rds-db:connect` | Database user `fintrust_score` only | Deployed W13 D2 |
| | `dynamodb:PutItem`, `dynamodb:UpdateItem` | Events table | Deployed W13 D2 |
| | `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` | Transactions FIFO queue | Deployed W13 D3 |
| | `kms:Decrypt` | The project key | Deployed W13 D3 |
| **alert** (`fintrust-capstone-alert-role`) | Logs, X-Ray, SSM (as ingest) | Own log group; `/fintrust/dev/*` | Deployed W13 D1 |
| | `dynamodb:GetRecords`, `GetShardIterator`, `DescribeStream` | Events table stream | Deployed W14 D1 |
| | `dynamodb:ListStreams` | `*` (AWS does not support a narrower resource for this action) | Deployed W14 D1 |
| | `dynamodb:GetItem`, `dynamodb:PutItem` | Events table (alert markers and journey steps) | Deployed W14 D1 |
| | `ses:SendEmail` | The verified sender and recipient identities only | Deployed W14 D1 |
| | `sqs:SendMessage` | Alert dead-letter queue only | Deployed W14 D1 |
| | `kms:Decrypt`, `kms:GenerateDataKey` | The project key | Deployed W14 D1 |
| **query** (`fintrust-capstone-query-role`) | Logs, X-Ray, network interfaces (as score) | Own log group | Deployed W13 D1 |
| | `rds-db:connect` | Database user `fintrust_query` only | Deployed W13 D2 |
| | `dynamodb:Query`, `dynamodb:GetItem` | Events table and its index | Deployed W13 D2 |
| | `s3:PutObject` | Raw bucket, `uploads/` prefix only | Deployed W14 D1 |
| | `kms:GenerateDataKey` | The project key | Deployed W14 D1 |
| | `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem` | Sessions table only | Deployed W15 D2 |
| **report** (`fintrust-capstone-report-role`) | Logs, X-Ray, network interfaces (as score) | Own log group | Deployed W13 D1 |
| | `rds-db:connect` | Database user `fintrust_report` only | Deployed W13 D2 |
| | `s3:PutObject` | Reports bucket, `monthly/` prefix only | Deployed W14 D4 |
| | `kms:GenerateDataKey` | The project key | Deployed W14 D4 |

What no role can do: delete anything, read another function's logs, change IAM, create or modify infrastructure, or reach any resource outside the `fintrust-capstone-` prefix.

### Where `Resource: "*"` is used, and why

| Statement | Actions | Reason |
|---|---|---|
| `XRayTracing` | `xray:PutTraceSegments`, `xray:PutTelemetryRecords` | X-Ray has no resource-level permissions |
| `VpcNetworkInterfaces` | Six `ec2:` network interface actions | Lambda must create its network interface before any resource ARN exists. Same actions as the AWS policy `AWSLambdaVPCAccessExecutionRole`, written out so nothing extra is granted |

### Permission boundary

`fintrust-capstone-permission-boundary` is attached to all five roles. A permission boundary is a ceiling: a role's effective permissions are only what **both** its own policy **and** the boundary allow. If someone later attaches a broad policy such as `AdministratorAccess` to a Lambda role by mistake, the boundary still limits it to project resources and the listed actions.

### Database users (second layer, inside PostgreSQL)

| Database user | Used by | Rights |
|---|---|---|
| `fintrust_score` | score Lambda | Insert into `transactions`, `risk_events`, `alerts`; read `accounts` and `transactions` |
| `fintrust_query` | query Lambda | Read all tables and both views; update the four resolution columns of `alerts`; execute `flag_account` |
| `fintrust_report` | report Lambda | Read only, and not the `customers` table: `accounts`, `transactions`, `risk_events`, `alerts` and both views |
| master user | The administrator, through the tunnel only | Schema changes. Never used by a Lambda |

So even if the report Lambda's code were compromised, both IAM and the database would refuse a write, and it could not read a customer's name or contact details.

The views `v_risk_dashboard` and `v_open_alerts` contain no customer contact details, so access to them can be granted without access to `customers`.

## Network security

| Control | Setting |
|---|---|
| VPC | Two private subnets in two Availability Zones. No internet gateway route, no NAT Gateway |
| RDS | `PubliclyAccessible = false`, in the private subnets |
| RDS security group | Inbound PostgreSQL 5432 only from the Lambda security group and the tunnel instance's security group |
| Lambda security group | No inbound rules. Outbound 5432 to the RDS security group, and HTTPS to the S3 and DynamoDB gateway endpoints (by AWS prefix list). Nothing else |
| Gateway endpoints | S3 and DynamoDB traffic stays on the AWS network |
| API Gateway | The only public entry point for data; HTTPS only; API key required; a Cognito ID token required on every route except `GET /health`; throttled |

## Encryption

| Data | At rest | In transit |
|---|---|---|
| S3 buckets | SSE-KMS with the customer-managed key; bucket key enabled to reduce KMS calls | Bucket policy denies requests without TLS |
| DynamoDB | Encrypted by default with an AWS owned key. The events table holds no customer details; the sessions table holds a username, a display name, the role and times, never a token or password | HTTPS |
| Cognito | Passwords are never stored by FinTrust or seen by its code; Cognito keeps them hashed | HTTPS; tokens are signed (RS256) so they cannot be altered |
| SQS queues | KMS customer-managed key | HTTPS |
| SNS alarm topic | KMS customer-managed key | HTTPS |
| RDS | Storage encryption enabled | TLS required for every connection (`sslmode=require`; IAM authentication only works over TLS) |
| SSM parameters | Standard parameters hold no secrets | HTTPS |

The customer-managed key (`alias/fintrust-capstone`) has automatic yearly rotation enabled. Its key policy (`infrastructure/kms/key-policy.json`) has two statements. The first is the AWS default, which hands control to IAM in this account; which role may use the key, and how, is then written in that role's own policy next to its other permissions. The second lets the CloudWatch service use the key, so that an alarm can publish to the encrypted alarm topic; a service is not an IAM role, so the key policy is the only place this can be granted. Only `kms:Decrypt` and `kms:GenerateDataKey` are ever granted, and a test fails the build if any role is given a key-management action.

The bucket policy uses `"Action": "s3:*"` in a **Deny** statement (refuse anything not sent over TLS). A wildcard in a Deny removes access; it cannot grant any. The "no wildcard actions" rule applies to the Allow statements in the Lambda roles.

## Authentication summary

| Who | To what | How |
|---|---|---|
| Compliance staff | Dashboard and API | Their own Cognito account: password (12+ characters, upper and lower case, number, symbol) and an authenticator-app code at every sign-in. The ID token goes in `Authorization`; the session is checked on every request |
| Dashboard and scripts (the application) | API | API key in `x-api-key`, limited by a usage plan |
| Verification scripts | API | The `fintrust-automation` user, compliance-officer role, signed in with Cognito's admin flow using the administrator's IAM credentials; password and MFA secret encrypted in SSM, replaced on every deployment |
| Lambda | AWS services | Its execution role (temporary credentials issued by AWS) |
| Lambda | PostgreSQL | IAM database authentication: a signed token valid for 15 minutes |
| Administrator | AWS | IAM user with MFA; the root user is used for billing settings only |
| Administrator | PostgreSQL | Master user through an SSH tunnel via EC2 Instance Connect Endpoint; no public IP, no open inbound port |

## The web dashboard

| Control | How |
|---|---|
| Files are not public in S3 | All public access blocked; CloudFront reads the bucket through origin access control; the bucket policy names the one distribution |
| HTTPS only | CloudFront redirects http to https; HSTS header |
| Nobody sees data without signing in | Only the sign-in screen is shown until Cognito has signed the person in; every data route needs their token |
| Tokens | Kept in the browser tab's `sessionStorage` only; ID token 30 minutes, refresh token 8 hours, revoked at sign-out |
| Script injection | No HTML is built from data; a strict Content-Security-Policy allows only the page's own script and only this API |
| Other websites cannot use the API through a visitor's browser | CORS allows the dashboard's address only, on Lambda responses and on API Gateway's own error responses |

Details are in [dashboard.md](dashboard.md).

## People: sign-in, roles and sessions (Week 15 Day 2)

The data is confidential, so every person has their own account. An API key alone could not say who resolved an alert.

| Control | Setting | Why |
|---|---|---|
| Identity provider | Amazon Cognito user pool `fintrust-capstone-users`, Essentials plan | A managed AWS service: FinTrust never stores or sees a password |
| Who can get an account | Administrators only (`scripts/manage_users.py create`); no self sign-up | A bank decides who has access |
| Password policy | At least 12 characters with upper and lower case, a number and a symbol; the last 5 cannot be reused; temporary passwords expire in 3 days | Length matters most against guessing; blocking reuse stops an old, leaked password coming back |
| MFA | Required for everyone, by authenticator app (TOTP) | A stolen password alone signs nobody in. SMS is not used: it costs money and can be intercepted |
| Account enumeration | Cognito answers the same way whether or not a username exists | An attacker cannot find valid usernames |
| Sign-in flow | OAuth 2.0 authorization code with PKCE, through Cognito's managed login pages | The dashboard cannot keep a client secret; PKCE makes a stolen code useless |
| Token lifetimes | ID token 30 minutes, refresh token 8 hours, token revocation on | A copied token is short-lived; signing out revokes the refresh token |
| Session | One record per sign-in in DynamoDB; ends on sign-out, after 15 minutes idle, after 8 hours, or when an administrator ends it | A JWT cannot be cancelled; the session record can |
| Roles | Cognito groups `compliance-officer` (may resolve and flag) and `compliance-analyst` (read and upload) | Separation of duties: not everyone who can read can change the record |
| Enforcement | The role is checked by the query Lambda on every route, not only hidden in the page | The page is not a security boundary |
| Audit | `resolved_by` and `flagged_by` are the signed-in username, taken from the token, never from the request; the query Lambda's logs name the actor on every request, refused ones included | Every decision is attributable, which POPIA's accountability principle and a bank's audit require |
| Leavers and lost laptops | `manage_users.py disable` (cannot sign in, every session ends) and `end-sessions` (signed out everywhere, refresh tokens revoked) | Access is removed at once, and the audit trail still points at a real account |

## Logging, monitoring and audit

- Structured JSON logs from every Lambda, 30-day retention.
- Logs never contain customer names, contact details or full account numbers.
- Two saved Logs Insights queries for people (Week 15 Day 2): who resolved, flagged or uploaded what, and refused requests by person.
- Seven CloudWatch alarms: scoring error rate, high-risk transaction count, the three dead-letter queues, Lambda errors and API server errors. Each emails an encrypted SNS topic. See [monitoring.md](monitoring.md).
- Custom metrics are published as log lines, so no Lambda role needs `cloudwatch:PutMetricData`.
- **CloudWatch** answers "is the system healthy?" (operations). **CloudTrail** answers "who changed what in the account?" (audit). CloudTrail's 90-day event history is on by default and is the audit record for IAM and infrastructure changes.

## Secrets handling in the repository

`.env` is in `.gitignore`; only `.env.example` with placeholders is committed. Rendered policy files that contain the account id are git-ignored. An automated test fails if a 12-digit account id appears in a policy template.
