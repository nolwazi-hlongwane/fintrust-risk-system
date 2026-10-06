# FinTrust Transaction Insights and Risk Monitoring System

Capstone project for the Cloud to Solutions Accelerator (Praesignis), built for the fictional client **FinTrust Bank SA**.

## What this system does, in plain language

Banks must watch their customers' transactions for signs of fraud or money laundering. At FinTrust this was done by hand from exported files, which is slow and easy to get wrong.

This system does it automatically:

1. A compliance officer **uploads a file** of transactions.
2. The system **checks every line**. Lines with mistakes are set aside with the reason, so they can be corrected.
3. Each good transaction gets a **risk score** from clear rules, for example "amount above R50 000" or "sent outside South Africa". The score always shows which rules produced it.
4. Everything is **saved securely** in the bank's ledger.
5. If a transaction is high risk, the compliance team **gets an email** within seconds.
6. Staff can **search and review** transactions and alerts on a dashboard.
7. Once a month, a **summary report** is produced for executives.

If any step fails, nothing is lost: the transaction waits in a holding area until the problem is fixed, then continues.

It runs entirely on Amazon Web Services (AWS) with no servers to manage, and is designed to cost nothing beyond the AWS Free Tier.

## Project status

| Brief slot | Phase | Status |
|---|---|---|
| Week 13 Day 1 | Design Kickoff and Infrastructure Setup | Done |
| Week 13 Day 2 | Data Layer | Done |
| Week 13 Day 3 | Security and Storage | Done |
| Week 13 Day 4 | ETL Lambda | Done |
| Week 13 Day 5 | API Backend Part 1 | Next |
| Week 14 Day 1 | Write API and Alert System | |
| Week 14 Day 2 | CloudWatch Monitoring and Observability | |
| Week 14 Day 3 | Automated Testing | |
| Week 14 Day 4 | SQL Analytics and Reporting Layer | |
| Week 14 Day 5 | End-to-End Integration and Submission | |

Progress and evidence for each phase are recorded in [docs/build-log.md](docs/build-log.md).

## Documents

| Document | What it covers |
|---|---|
| [Project execution plan](docs/project-execution-plan.md) | Scope, phases, decisions |
| [Architecture](docs/architecture.md) | Diagram, data flow, why each AWS service was chosen |
| [API design](docs/api-design.md) | Every route, request, response and error |
| [Security plan and IAM role matrix](docs/security-plan.md) | Who and what can access which resource |
| [Data management design](docs/data-governance.md) | POPIA, data quality, retention, migration, risk register |
| [ERD](docs/erd.md) | Database tables and relationships |
| [SQL](sql/) | `schema.sql`, `db_users.sql`, `seed.sql` |
| [Logs Insights queries](docs/logs-insights-queries.md) | Ready-made queries for the structured logs |
| [Build log](docs/build-log.md) | What was built in each phase, with test results |

## Repository layout

```
fintrust/            The Python package (one module per responsibility)
tests/unit/          Fast tests that use a fake AWS (moto); run on every push
tests/integration/   Tests that need a PostgreSQL container; run on develop only
sql/                 Database schema, seed data, reports, views, functions
infrastructure/      Scripts and policy files that create the AWS resources
scripts/             Operational scripts (end-to-end test, DLQ redrive, RDS stop/start)
frontend/            The compliance dashboard (HTML, CSS, JavaScript)
data/                Sample transaction CSV files
docs/                Design documents, diagrams and evidence screenshots
.github/workflows/   Continuous integration (GitHub Actions)
```

## Run the tests on your own computer

You need Python 3.13.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The tests use a simulated AWS, so they need no AWS account and cost nothing.

The integration tests also need a PostgreSQL database. With Docker running:

```bash
docker compose up -d
PGHOST=localhost PGPORT=5433 PGUSER=postgres PGPASSWORD=local-test-only pytest
docker compose down
```

Without a database those tests are skipped, not failed.

## Deploy to AWS

You need an AWS account, the AWS CLI configured for an **IAM user** (not the root user), and the region set to `eu-north-1`. Each step is a script that is safe to run more than once. Steps are added here as each phase is completed.

### Step 1: roles, configuration and billing alarm (Week 13 Day 1)

```bash
# Check who you are signed in as. It must NOT end in ":root".
aws sts get-caller-identity

# Create the five Lambda execution roles and their permission boundary
./infrastructure/iam/deploy_roles.sh

# Store the application settings in SSM Parameter Store
ALERT_EMAIL=you@example.com ./infrastructure/ssm/put_parameters.sh

# Create the billing alarm (email if charges pass 11 USD, about R200)
ALERT_EMAIL=you@example.com ./infrastructure/billing/create_billing_alarm.sh
```

Then open the email from AWS Notifications and choose **Confirm subscription**.

### Step 2: network, database and event store (Week 13 Day 2)

```bash
./infrastructure/network/deploy_network.sh     # private network, about 1 minute
./infrastructure/rds/deploy_rds.sh             # PostgreSQL database, about 10 minutes
./infrastructure/dynamodb/deploy_table.sh      # DynamoDB table
./infrastructure/iam/apply_data_policies.sh    # let each role reach only its own data
./infrastructure/rds/deploy_tunnel.sh          # private access for the administrator, about 5 minutes
```

The database has no public address. To load the schema, open the private tunnel in a **second** terminal window and leave it running:

```bash
./scripts/db_tunnel.sh
```

Then, back in the first window:

```bash
python scripts/run_sql.py sql/schema.sql sql/db_users.sql sql/seed.sql
python scripts/run_sql.py -c "SELECT risk_level, count(*) FROM transactions GROUP BY 1 ORDER BY 1"
```

When you finish for the day, stop the database so it does not use credits:

```bash
./scripts/rds_stop.sh      # and ./scripts/rds_start.sh to resume
```

### Step 3: encryption key, buckets and queues (Week 13 Day 3)

The database does not need to be running for this step.

```bash
./infrastructure/kms/deploy_key.sh             # the project's encryption key
./infrastructure/s3/deploy_buckets.sh          # four encrypted, private buckets
./infrastructure/sqs/deploy_queues.sh          # transaction queue and dead-letter queues
./infrastructure/iam/apply_data_policies.sh    # let each role reach only its own data
python scripts/verify_storage.py               # prove every control is really in place
```

The last command checks the live account and finishes with a line such as `43 of 43 checks passed`.

### Step 4: the ingest (ETL) Lambda (Week 13 Day 4)

The database does not need to be running for this step.

```bash
./infrastructure/dynamodb/deploy_table.sh         # raise write capacity (still inside the Free Tier)
./infrastructure/lambda/deploy_function.sh ingest # package, deploy, wire the S3 trigger and dead-letter queue
python scripts/verify_ingest.py                   # upload a sample file and check what the pipeline did
```

To try it yourself, upload a file and watch the logs:

```bash
aws s3 cp data/sample_transactions_valid.csv s3://fintrust-capstone-raw-$(aws sts get-caller-identity --query Account --output text)/uploads/demo/valid.csv
aws logs tail /aws/lambda/fintrust-capstone-ingest --since 5m
```

## Sample data

| File | Contents |
|---|---|
| `data/sample_transactions_valid.csv` | 15 valid transactions across 9 accounts |
| `data/sample_transactions_mixed.csv` | 11 rows: 4 valid and 7 with deliberate errors, one per validation rule |

CSV columns: `transaction_id, account_id, tx_date, amount, currency, tx_type, channel, merchant_name, country_code, description`. `currency`, `merchant_name` and `description` are optional.

## Branching and commits

- `main`: reviewed, working releases. `develop`: integration branch. `feature/<name>`: one piece of work, merged into `develop` by pull request.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `feat(config): load settings from SSM Parameter Store`.

## Author

Nolwazi Hlongwane, Cloud to Solutions Accelerator, 2026.
