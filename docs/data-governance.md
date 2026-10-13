# Data Management Design Document

**Brief slot:** Week 13 Day 1: Design Kickoff (first draft). Finalised alongside the build in Week 14 Day 5.
**Status:** final. Field names match the deployed schema (a test compares the ERD with the database). A one-page summary is in [popia-lifecycle-statement.md](popia-lifecycle-statement.md).

This document covers the five data management elements required by the brief: POPIA classification, data quality, lifecycle, migration, and the risk register.

---

## 1. POPIA data classification

The Protection of Personal Information Act (POPIA) protects information that identifies a living person. A bank account number or a transaction linked to an account identifies its holder, so the ledger is personal information even though it holds no names.

### Classification levels used

| Level | Meaning | Handling |
|---|---|---|
| **Personal (direct)** | Identifies a person on its own | Stored only in `customers`; never logged; never placed in DynamoDB, SQS messages or emails |
| **Personal (financial)** | Financial information linked to a person through the account | Encrypted at rest and in transit; access by role |
| **Internal** | Operational data with no personal link on its own | Standard controls |

### Fields in the `transactions` table

| Field | Classification | Why |
|---|---|---|
| `transaction_id` | Internal | System identifier; safe to log |
| `account_id` | Personal (financial) | Links the row to a person |
| `tx_date` | Personal (financial) | Reveals behaviour when combined with the account |
| `amount`, `currency`, `tx_type` | Personal (financial) | Financial history of an identifiable person |
| `channel`, `merchant_name`, `country_code`, `description` | Personal (financial) | Reveal location and spending habits |
| `risk_score`, `risk_level` | Personal (financial) | An assessment made about a person; POPIA section 71 covers automated decisions, so every score stores the rules that produced it and a human reviews every alert |
| `source_file`, `created_at` | Internal | Lineage |

Directly identifying fields (`full_name`, `email`, `phone_number`) exist only in the `customers` table.

### Staff sign-in data (Week 15 Day 2)

Signing in creates personal information about **staff**, not customers. It is classified and limited in the same way.

| Data | Where | Classification | Kept for |
|---|---|---|---|
| Staff name, work email, username, password hash, MFA secret | Amazon Cognito user pool | Personal (direct, staff) | While the person has access; disabled, not deleted, when they leave, so the audit trail still resolves |
| Session record: username, display name, role, sign-in times, browser | DynamoDB `fintrust-capstone-sessions` | Personal (staff activity) | 90 days, then deleted by time-to-live |
| `resolved_by`, `flagged_by` | RDS `alerts`, `risk_events` | Internal: a username, never a name or email | With the record it belongs to |
| Actor on each API log line | CloudWatch Logs | Internal: a username | 30 days |

### How it is stored, who can access it

| Question | Answer |
|---|---|
| How is it stored? | RDS PostgreSQL in private subnets with storage encryption; S3 and SQS encrypted with a customer-managed KMS key; DynamoDB encrypted at rest by default; TLS on every connection |
| Who can access it? | Named compliance staff, each signed in with their own Cognito account and MFA, through the API (which also needs the application's API key). Officers may resolve alerts and flag accounts; analysts may only read and upload. Each Lambda only through its own least-privilege role and its own database user. No direct database access from the internet |
| What leaves the system? | Alert emails carry the transaction id, a masked account id, the score and the rules. No names or contact details |
| How long is it kept? | See section 3 |

### POPIA conditions and where each is met

| Condition | How this system meets it |
|---|---|
| Purpose specification | Data is processed only to monitor transaction risk, a regulatory duty |
| Minimality | The CSV carries account ids, not customer names; the event store holds no direct identifiers |
| Information quality | Row-level validation and quarantine (section 2) |
| Security safeguards (section 19) | Encryption, least privilege, private networking, logging; each person signs in with MFA, sessions end after 15 idle minutes, and access is removed at once when someone leaves |
| Accountability (section 8) | Every decision that changes the record (resolving an alert, flagging an account) is stored with the username of the signed-in officer, taken from their sign-in, not typed in |
| Retention limits (section 14) | Lifecycle policy with fixed periods (section 3) |
| Cross-border transfer (section 72) | Data is hosted in the AWS Stockholm region. Sweden applies the EU GDPR, which gives protection substantially similar to POPIA. A production deployment for a South African bank would use the Cape Town region (af-south-1) to keep data in the country |

---

## 2. Data quality framework

### Validation rules at ingestion

Every row is checked before anything is stored. Rules are applied in `fintrust/validators.py`.

| Dimension | Rule |
|---|---|
| Completeness | `transaction_id`, `account_id`, `tx_date`, `amount`, `tx_type` must be present and not blank |
| Validity | `amount` is a positive number with at most two decimals; `tx_type` is `CREDIT` or `DEBIT`; `currency` is a three-letter code; `country_code` is a two-letter code; `tx_date` is a real date and time |
| Timeliness | `tx_date` is not in the future |
| Uniqueness | A `transaction_id` may appear once per file; the queue's deduplication and the ledger's primary key stop the same transaction being stored twice |
| Consistency | `transaction_id` and `account_id` match the expected formats; the account must exist in the ledger (enforced by a foreign key when the transaction is scored) |
| Sanitisation | Leading and trailing spaces removed; control characters removed; text fields limited in length; values that could be read as spreadsheet formulas are neutralised |
| POPIA minimality | Columns the system does not need (for example a customer name) are never read. Email addresses, phone numbers and ID or card numbers found in free-text fields are replaced with `[REDACTED]` before the row is stored, queued or logged |
| File-level checks | UTF-8 text, a header containing every required column, at most 10 MB and 5,000 rows |

A row that fails is reported with **every** reason, not only the first, so it can be corrected in one pass.

### What happens to records that fail

| Failure | Destination | Who is told |
|---|---|---|
| A row fails validation | Written to the **S3 quarantine bucket** with the row number and every reason; valid rows in the same file continue | Counted in the `RowsQuarantined` metric; visible on the dashboard |
| A whole file is unusable (not UTF-8, wrong header, too large) | A rejection report naming the file and the reason is written to the quarantine bucket; nothing is queued | Error log and the error-rate alarm |
| The ingest function itself fails | Lambda retries twice, then sends the S3 event to the **ingest dead-letter queue** | Error log and the error-rate alarm |
| A valid message cannot be scored after 3 attempts | **SQS dead-letter queue**, kept for 14 days | CloudWatch alarm on DLQ depth emails the data steward |
| The cause is fixed | DLQ is redriven to the main queue and processed normally | Recorded in the transaction's journey |

No record is silently dropped: every row ends in the ledger, in quarantine, or in the DLQ, and the counts are logged so they can be reconciled against the file's row count.

---

## 3. Data lifecycle policy

| Tier | Where | Period | Why |
|---|---|---|---|
| **Hot** | RDS PostgreSQL | 12 months | Recent transactions need fast queries for monitoring and the API |
| **Warm** | S3 (processed bucket, Standard-Infrequent Access; Parquet in production) | Up to 3 years | Occasional investigation; cheaper storage |
| **Cold** | S3 Glacier | Until 7 years from the transaction date, then deleted | FSCA regulatory record-keeping requirement |

How it is enforced:

- **S3 Lifecycle rules** move objects between storage classes and expire them at 7 years. This is automatic; nobody has to remember to do it.
- **S3 Versioning** on all four buckets protects records from accidental overwrite or deletion during the retention period.
- **Quarantine bucket:** 90 days, long enough to correct and resubmit, short enough not to keep faulty personal data.
- **CloudWatch Logs:** 30 days. Logs carry identifiers only.
- **DynamoDB events:** 12 months, removed with a time-to-live attribute.
- **DynamoDB sessions:** 90 days, removed with a time-to-live attribute: long enough to answer "who was signed in when?", no longer.
- **RDS:** rows older than 12 months are exported to S3 and then deleted (documented procedure; not automated in the capstone).

---

## 4. Migration strategy

**Scenario:** FinTrust's legacy system writes transaction exports as flat files to a network-attached storage (NAS) server in its data centre.

| Item | Decision |
|---|---|
| **7 Rs classification** | **Replatform** ("lift, tinker and shift") |
| **AWS service** | **AWS Application Migration Service (MGN)** for the legacy file server |
| **Supporting services** | AWS DataSync for the historical flat files on the NAS; AWS Database Migration Service (DMS) if a legacy database is found during discovery |

**Why Replatform and not Rehost.** A pure Rehost would copy the file server to an EC2 instance unchanged. That is fastest, but it keeps the manual processing that is the business problem. Replatform keeps the same data and the same business process (transaction files are produced, then checked), while swapping the storage for S3 and the manual checks for managed services. It gains durability, encryption and automation without rewriting the core banking system that produces the files.

**Why not Refactor.** Re-architecting the legacy core banking system is high risk and out of scope. It can happen later, behind the same S3 interface.

**Steps.**

1. **Assess:** inventory the NAS (file count, sizes, formats, owners) and classify the data under POPIA.
2. **Replicate:** Application Migration Service continuously replicates the legacy server to AWS so cutover can be rehearsed; DataSync copies the historical files into the raw bucket over an encrypted connection, verifying checksums.
3. **Validate:** run the historical files through this pipeline; reconcile row counts and totals against the legacy system; review quarantined rows.
4. **Parallel run:** both systems process the same files for one month.
5. **Cut over:** the legacy export job writes to S3; the NAS becomes read-only.
6. **Retire:** decommission the NAS after the retention review.

---

## 5. Risk register

| # | Data governance risk | Probability | Impact | Named AWS control |
|---|---|---|---|---|
| 1 | **Unauthorised access to personal financial information** in the ledger, through an over-permissive role or an exposed database | Medium | High: POPIA breach, notification to the Information Regulator, reputational damage | **AWS IAM** least-privilege roles with a **permission boundary**, plus RDS in **private VPC subnets** with security groups and **IAM database authentication** |
| 2 | **Poor-quality or corrupt data** enters the ledger and produces wrong risk decisions or missed alerts | High | Medium: wrong alerts, compliance findings | Validation in **AWS Lambda** with the **S3 quarantine bucket**, an **SQS dead-letter queue**, and a **CloudWatch alarm** on failures |
| 3 | **Records are lost, altered or deleted before the 7-year retention period ends**, or kept longer than allowed | Low | High: FSCA non-compliance, POPIA retention breach | **S3 Versioning** and **S3 Lifecycle rules** to **S3 Glacier**, encryption with **AWS KMS**, and **RDS automated backups** |

Review: the register is reviewed at each release and after any incident. Changes to IAM and infrastructure are traceable in **AWS CloudTrail**.
