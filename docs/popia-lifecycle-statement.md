# Data Lifecycle and POPIA Compliance Statement

**FinTrust Transaction Insights and Risk Monitoring System** · one-page summary of the [data management design document](data-governance.md)

## What personal information the system holds

A transaction linked to an account identifies the account holder, so the ledger is personal information under POPIA even though the transaction files carry no names.

| Classification | Fields | Where it may exist |
|---|---|---|
| **Personal (direct)** | `full_name`, `email`, `phone_number` | The `customers` table only. Never in logs, queues, the event store, emails, reports or the dashboard |
| **Personal (financial)** | `account_id`, `tx_date`, `amount`, `currency`, `tx_type`, `channel`, `merchant_name`, `country_code`, `description`, `risk_score`, `risk_level` | Ledger (RDS), transaction files (S3), queue messages (SQS). Account ids and scores also in the event store (DynamoDB) |
| **Internal** | `transaction_id`, `source_file`, `created_at` | Anywhere, including logs |

## Retention tiers

| Tier | Store | Kept for | Enforced by |
|---|---|---|---|
| **Hot** | RDS PostgreSQL | 12 months | Documented export-and-delete procedure |
| **Warm** | S3 Standard-Infrequent Access (after 30 days) | Up to 3 years | S3 Lifecycle rule |
| **Cold** | S3 Glacier | Until 7 years from creation, then deleted (FSCA record-keeping) | S3 Lifecycle rule |
| Quarantined rows | S3 quarantine bucket | 90 days | S3 Lifecycle rule |
| Scores and journey events | DynamoDB | 12 months | Time-to-live attribute |
| Staff sign-in sessions | DynamoDB | 90 days | Time-to-live attribute |
| Logs | CloudWatch Logs | 30 days | Log group retention |

Versioning on every bucket protects records from overwrite or deletion during their retention period.

## Access controls

| Who or what | Access | Control |
|---|---|---|
| Compliance officers | Read transactions, alerts and accounts; upload files; resolve alerts; flag accounts | Own Cognito account with MFA; session ends after 15 idle minutes; decisions recorded under their username; HTTPS only |
| Compliance analysts | Read and upload only | As above; the API refuses resolve and flag for this role |
| Each Lambda function | Only the actions and resources its job needs | One least-privilege IAM role each, under a permission boundary; no wildcard actions |
| Each Lambda inside the database | score: insert only. query: read, resolve alerts, flag through one function. report: read only, and not the `customers` table | Three PostgreSQL users with IAM database authentication; no password exists |
| The internet | No access to the database | Private subnets, no internet gateway, no public endpoint |
| The administrator | Schema changes only | Private tunnel authorised by IAM and recorded in CloudTrail |

## How the POPIA conditions are met

| Condition | In this system |
|---|---|
| Purpose specification | Processed only to monitor transaction risk, a regulatory duty |
| Minimality | Files carry account ids, not names; unneeded columns are never read; personal details found in free text are redacted at ingestion |
| Information quality | Every row is validated; failures are quarantined with every reason, never stored |
| Security safeguards (s19) | Encryption at rest with AWS KMS and in transit with TLS; least privilege at the IAM, network and database layers; monitoring and alarms |
| Retention (s14) | Fixed periods, deleted automatically at the end |
| Automated decisions (s71) | Every score stores the rules that produced it; a person reviews every alert; nothing is blocked automatically |
| Cross-border transfer (s72) | Hosted in Stockholm under the EU GDPR, which gives comparable protection; a production deployment would use Cape Town (af-south-1) |

## Governance risks

The three named risks, each with probability, impact and a named AWS control, are in the [risk register](data-governance.md#5-risk-register).
