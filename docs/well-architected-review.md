# Well-Architected Review: Self-Assessment

**Brief slot:** Week 14 Day 5: End-to-End Integration and Submission
**Framework:** AWS Well-Architected Framework, all six pillars
**Scope:** the FinTrust system as deployed in my AWS account, region eu-north-1 (Stockholm)

This is an honest review, not a sales document. For each pillar it lists what the design does well, where it knowingly falls short and why, and what would change for production. Each "gap" is a decision that was made on purpose under the brief's constraints (Free Tier, no NAT Gateway, two build weeks), not something overlooked.

## Summary

| Pillar | Rating | The strongest point | The main gap |
|---|---|---|---|
| Operational excellence | Good | Everything is deployed by repeatable scripts and verified by scripts | Scripts, not Infrastructure as Code; deployments are run by hand |
| Security | Strong | Least privilege at three layers: IAM, network, database | API key identifies an application, not a person |
| Reliability | Good | Nothing is lost on failure; proven by a break-and-recover test | The database is single-AZ |
| Performance efficiency | Good for the load | Purpose-built stores; reused connections | Cold starts in the VPC; offset pagination |
| Cost optimisation | Strong | Idle cost is close to zero; the one always-on resource is stopped nightly | KMS key and database storage draw a small amount of credit |
| Sustainability | Good | Serverless: no idle servers | Region chosen for the course, not for carbon or latency |

## 1. Operational excellence

*Run and monitor systems, and keep improving them.*

**What is in place**

- **Repeatable deployment.** Every resource is created by a script in `infrastructure/` that is safe to run again. Nothing was clicked together in the console.
- **Verification after every change.** Eight scripts check the real account: `verify_storage`, `verify_ingest`, `verify_scoring_api`, `verify_write_api_alerts`, `verify_monitoring`, `verify_report`, `verify_dashboard` and the full `e2e_test`.
- **Observability.** Structured JSON logs, nine custom metrics, seven alarms that email, one dashboard, ten saved Logs Insights queries and X-Ray tracing (`docs/monitoring.md`).
- **Runbook thinking.** Each alarm's description says what to do, and `docs/monitoring.md` has a "what to do" column for each.
- **Small, reviewed changes.** Feature branches, pull requests into `develop`, CI on every push, Conventional Commits.

**Gaps and what I would change**

| Gap | Why it is this way | Production change |
|---|---|---|
| Shell scripts instead of CloudFormation, SAM or Terraform | The brief says "deployed via console or CLI"; scripts are easy to read line by line | Infrastructure as Code, so drift is detectable and environments can be created and destroyed as a unit |
| Deployment is manual | No AWS credentials are stored in GitHub | A deploy job using GitHub OIDC to assume a short-lived role |
| One environment (`dev`) | Free Tier | Separate accounts for dev, test and production under AWS Organizations |

## 2. Security

*Protect data, systems and assets.*

**What is in place**

- **Identity.** One IAM role per Lambda. No wildcard actions (a test fails the build if one appears). `Resource: "*"` only in four named cases where AWS offers nothing narrower. A permission boundary on all five roles caps what any of them could ever be given.
- **Three layers of least privilege.** IAM decides which role may call what. Security groups allow the VPC Lambdas to reach the database on port 5432 and nothing else. Inside PostgreSQL each Lambda has its own user: the score user can insert but never update or delete; the report user is read-only and cannot read customer details; the API can change an account only through one audited function.
- **No database password in any Lambda.** IAM database authentication: a 15-minute token signed by the role.
- **No public database.** Private subnets, no internet gateway, no NAT Gateway. Administration goes through an EC2 Instance Connect Endpoint to an instance with no public address.
- **Encryption.** A customer-managed KMS key with rotation for the data buckets, queues and alarm topic; RDS and DynamoDB encrypted at rest; TLS everywhere, enforced by bucket policy.
- **Data protection.** Personal contact details exist only in one table. Emails, logs, metrics, the event store and reports carry identifiers only; account numbers are masked in emails and logs; free text is scanned and redacted at ingestion.
- **Application security.** Every input is validated before any query; SQL is always parameterised; errors never return internal detail; the dashboard never builds HTML from data and runs under a strict Content-Security-Policy; CORS allows one origin.

**Gaps and what I would change**

| Gap | Why it is this way | Production change |
|---|---|---|
| API key authentication | Specified by the brief | Amazon Cognito or the bank's identity provider, with a token per user and roles (viewer, officer, supervisor) |
| No AWS WAF | Not in the Free Tier | WAF on CloudFront and API Gateway |
| Data hosted in Stockholm | The region used throughout the course | Cape Town (af-south-1) to keep data in South Africa |
| No GuardDuty, Security Hub or AWS Config | Cost and scope | Enable all three, organisation-wide |
| DynamoDB uses the AWS-owned key | It holds no direct identifiers; the customer-managed key is used where files and messages hold transaction detail | Customer-managed key on the table as well |

## 3. Reliability

*Recover from failure and keep working.*

**What is in place**

- **Nothing is lost.** A failed scoring attempt rolls back its database transaction and the message stays on the queue. After three attempts it is kept in a dead-letter queue for 14 days. Failed files and failed alert emails have their own dead-letter queues.
- **Proven, not assumed.** `scripts/e2e_test.py` makes a transaction fail on the real account, watches it reach the dead-letter queue, fixes the cause, redrives the queue and checks the transaction is scored. The same scenario runs in the automated tests.
- **Failure is contained.** FIFO message groups mean one account's stuck message never delays another account. An email failure can never undo a ledger write.
- **Safe to repeat.** A re-uploaded file, a redelivered message and a repeated stream record each produce no duplicate row, alert or email.
- **Loose coupling.** A queue between ingest and scoring absorbs bursts and a database outage.
- **Backups and versioning.** RDS automated backups; versioning on every bucket.

**Availability Zone failure (the brief's resilience requirement)**

| Component | If one Availability Zone fails |
|---|---|
| Lambda (all five) | Keeps running. The VPC functions are attached to subnets in two zones; the others run across zones automatically |
| SQS, DynamoDB, S3, API Gateway, SES, EventBridge, CloudFront | Keep running. Each is a regional or global service spread across zones by AWS |
| RDS PostgreSQL | **Unavailable if its zone is the one that fails.** This is the one single point of failure |

What happens while the database is down: uploads are still accepted and validated; messages wait on the queue; failed attempts retry and then wait in the dead-letter queue; the API answers 503 for routes that need the ledger. When the database returns, a redrive processes everything. No transaction is lost; processing is delayed.

**Gaps and what I would change**

| Gap | Why it is this way | Production change |
|---|---|---|
| RDS is single-AZ | Multi-AZ is outside the Free Tier | RDS Multi-AZ with automatic failover, and RDS Proxy in front |
| One region | Scope | Cross-region backups, and a documented recovery time and recovery point |
| Redrive is a manual command | A person should confirm the cause is fixed first | Keep it manual, but add it to an incident runbook with an owner |

## 4. Performance efficiency

*Use resources efficiently as demand changes.*

**What is in place**

- **Purpose-built databases.** Relational PostgreSQL for the ledger, where joins, constraints and transactions matter. DynamoDB for scores and journeys, which are always read by a known key in single-digit milliseconds.
- **Connection reuse.** One database connection and one set of AWS clients per Lambda container, created outside the handler.
- **Indexes for the real queries.** `(account_id, tx_date)` serves both the API and the velocity rule.
- **Asynchronous by default.** The upload answers in milliseconds with 202; processing happens behind a queue.
- **Work is filtered early.** The alert Lambda is only invoked for HIGH scores, by a filter on the stream.
- **Caching at the edge.** CloudFront serves the dashboard, including from South African edge locations.

**Gaps and what I would change**

| Gap | Why it is this way | Production change |
|---|---|---|
| First request after idle is slow (cold start in a VPC) | Acceptable for an internal tool | Provisioned concurrency on the API Lambda |
| Offset pagination | Simple and fine for thousands of rows | Keyset pagination for millions |
| A db.t3.micro allows about 100 connections | Free Tier | RDS Proxy, and a reserved concurrency limit on the Lambdas |
| Not load tested | Scope | A load test at expected peak before go-live |

## 5. Cost optimisation

*Deliver value at the lowest price.*

**What is in place**

- **Serverless.** Lambda, API Gateway, SQS, DynamoDB, SES, EventBridge and CloudFront cost nothing when idle and are far inside their free allowances at this volume.
- **No NAT Gateway** (about 32 USD a month avoided). Gateway endpoints for S3 and DynamoDB are free; the design was shaped around that constraint.
- **No Secrets Manager** (IAM database authentication instead) and **no interface endpoints** (metrics are published through logs).
- **The database is stopped when not in use**, and stopped automatically every night by an EventBridge Scheduler schedule, because AWS restarts a stopped database after 7 days.
- **Storage lifecycle.** Objects move to cheaper storage classes automatically and expire at 7 years. Logs are kept 30 days.
- **Guard rails.** A billing alarm at about R200, and tests that fail the build if the project would exceed 10 custom metrics or 10 alarms.

**What draws on credits**

| Item | Approximate cost |
|---|---|
| RDS db.t3.micro while running, and its storage while stopped | The largest item; minimised by the nightly stop |
| The customer-managed KMS key | About 1 USD a month |
| The small EC2 instance for the database tunnel, while running | Stopped with the database |

Billing evidence is in `docs/evidence/` (see `docs/rubric-audit.md`).

**Production change:** Savings Plans or reserved capacity for the database once usage is known; cost allocation tags are already on every resource (`Project=fintrust-capstone`).

## 6. Sustainability

*Minimise environmental impact.*

**What is in place**

- **No idle compute.** Serverless services run only when there is work. The one server-like resource, the database, is stopped outside working hours.
- **Managed services** share infrastructure across many customers, which uses hardware more efficiently than dedicated servers.
- **Data is not kept longer than needed.** Lifecycle rules and time-to-live delete data at the end of its retention period.
- **Small payloads.** The dashboard is five small files with no framework.

**Gap:** the region was chosen for the course. A production choice would weigh latency to South African users, data residency and the region's energy mix together; Cape Town satisfies the first two.

## The three improvements I would make first

1. **RDS Multi-AZ with RDS Proxy.** It removes the only single point of failure and the connection limit.
2. **Per-user authentication with Amazon Cognito, and AWS WAF.** It closes the main security gap for a bank.
3. **Infrastructure as Code with an automated, OIDC-based deployment.** It makes every environment reproducible and every change reviewable.
