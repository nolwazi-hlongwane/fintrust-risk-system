# Security Plan and IAM Role Matrix

**Brief slot:** Week 13 Day 1: Design Kickoff and Infrastructure Setup · **Status:** roles and boundary deployed on Day 1 with base permissions; data permissions are added on the day each resource is created (see the Status column).

## Principles

1. **Least privilege.** One execution role per Lambda. Each role lists exact actions on exact resources. No wildcard actions anywhere; this is enforced by an automated test (`tests/unit/test_iam_policies.py`) that fails the build if one is added.
2. **Defence in depth.** A request must pass several independent controls: API key, IAM role, permission boundary, security group, database user rights, encryption.
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
| **report** (`fintrust-capstone-report-role`) | Logs, X-Ray, network interfaces (as score) | Own log group | Deployed W13 D1 |
| | `rds-db:connect` | Database user `fintrust_report` only | Deployed W13 D2 |
| | `s3:PutObject` | Reports bucket objects | W14 D4 |
| | `kms:GenerateDataKey` | The project key | W14 D4 |

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
| `fintrust_query` | query Lambda | Read all tables and views; update `alerts`; execute `flag_account` |
| `fintrust_report` | report Lambda | Read only |
| master user | Nolwazi, through the tunnel only | Schema changes. Never used by a Lambda |

So even if the report Lambda's code were compromised, both IAM and the database would refuse a write.

## Network security

| Control | Setting |
|---|---|
| VPC | Two private subnets in two Availability Zones. No internet gateway route, no NAT Gateway |
| RDS | `PubliclyAccessible = false`, in the private subnets |
| RDS security group | Inbound PostgreSQL 5432 only from the Lambda security group and the tunnel instance's security group |
| Lambda security group | No inbound rules. Outbound 5432 to the RDS security group, and HTTPS to the S3 and DynamoDB gateway endpoints (by AWS prefix list). Nothing else |
| Gateway endpoints | S3 and DynamoDB traffic stays on the AWS network |
| API Gateway | The only public entry point; HTTPS only; API key required; throttled |

## Encryption

| Data | At rest | In transit |
|---|---|---|
| S3 buckets | SSE-KMS with the customer-managed key; bucket key enabled to reduce KMS calls | Bucket policy denies requests without TLS |
| DynamoDB | Encrypted by default with an AWS owned key (no customer details are stored in this table) | HTTPS |
| SQS queues | KMS customer-managed key | HTTPS |
| RDS | Storage encryption enabled | TLS required for every connection (`sslmode=require`; IAM authentication only works over TLS) |
| SSM parameters | Standard parameters hold no secrets | HTTPS |

The customer-managed key (`alias/fintrust-capstone`) has automatic yearly rotation enabled. Its key policy is the AWS default, which hands control to IAM in this account; which role may use the key, and how, is then written in that role's own policy next to its other permissions. Only `kms:Decrypt` and `kms:GenerateDataKey` are ever granted, and a test fails the build if any role is given a key-management action.

The bucket policy uses `"Action": "s3:*"` in a **Deny** statement (refuse anything not sent over TLS). A wildcard in a Deny removes access; it cannot grant any. The "no wildcard actions" rule applies to the Allow statements in the Lambda roles.

## Authentication summary

| Who | To what | How |
|---|---|---|
| Compliance user | API | API key in `x-api-key`, limited by a usage plan |
| Lambda | AWS services | Its execution role (temporary credentials issued by AWS) |
| Lambda | PostgreSQL | IAM database authentication: a signed token valid for 15 minutes |
| Nolwazi | AWS | IAM user with MFA; root user is used for billing settings only |
| Nolwazi | PostgreSQL | Master user through an SSH tunnel via EC2 Instance Connect Endpoint; no public IP, no open inbound port |

## Logging, monitoring and audit

- Structured JSON logs from every Lambda, 30-day retention.
- Logs never contain customer names, contact details or full account numbers.
- CloudWatch alarms on error rate, high-risk transaction count and DLQ depth.
- **CloudWatch** answers "is the system healthy?" (operations). **CloudTrail** answers "who changed what in the account?" (audit). CloudTrail's 90-day event history is on by default and is the audit record for IAM and infrastructure changes.

## Secrets handling in the repository

`.env` is in `.gitignore`; only `.env.example` with placeholders is committed. Rendered policy files that contain the account id are git-ignored. An automated test fails if a 12-digit account id appears in a policy template.
