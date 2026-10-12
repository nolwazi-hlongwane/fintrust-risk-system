# Testing and CI/CD

**Brief slot:** Week 14 Day 3: Automated Testing
**Rubric:** C4 Testing and CI/CD

## The idea in one paragraph

Every change is checked automatically before it can reach the `develop` or `main` branch. Fast tests with a simulated AWS run on every push. Slower tests with a real PostgreSQL database run on the `develop` branch. Scripts that touch the real AWS account are run by hand after each deployment. Each layer catches a kind of mistake the layer before it cannot.

## The three layers

| Layer | What it runs against | When | Tests | What it proves |
|---|---|---|---|---|
| **Unit** (`tests/unit`) | moto (a simulated AWS, in memory) | Every push, any branch | 742 | Each module does its own job: validation rules, scoring rules, error handling, email content, API responses, IAM policies |
| **Integration** (`tests/integration`) | moto and a real PostgreSQL 16 | `develop` branch only | 203 | The modules work together and the SQL is right: constraints, rollback, permissions per database user, and the whole pipeline end to end |
| **Live verification** (`scripts/verify_*.py`, `scripts/e2e_test.py`) | The real AWS account | By hand, after each deployment | 8 scripts | The deployed system works: real IAM, real network, real email |

Coverage of the application code (`fintrust/`) is 99% with all tests, 96% with unit tests alone.

### Why moto

moto replaces AWS with an in-memory copy inside the test process. The code under test calls `boto3` exactly as it does in Lambda, and moto answers. So the tests need no AWS account, cost nothing, and run in seconds. Fake credentials are set before any project module is imported (top of `tests/conftest.py`), so a test can never reach a real account by accident.

### Why a real PostgreSQL for integration tests

moto cannot imitate a database engine. Constraints, foreign keys, transactions, the stored function and per-user permissions only mean something on a real PostgreSQL. In CI, GitHub starts a throwaway PostgreSQL container for the job; locally, `docker compose up -d` does the same.

## Shared fixtures (`tests/conftest.py`)

A fixture is setup code that pytest runs for any test that asks for it by name.

| Fixture | What it gives a test |
|---|---|
| `aws` | moto switched on for every AWS service (`mock_aws`) |
| `ssm`, `put_parameter` | An empty Parameter Store and a helper to add settings |
| `events_table` | The DynamoDB events table with the same keys and index as the deployed one |
| `pipeline` | The three S3 buckets, the FIFO queue and the events table, with config pointing at them |
| `mail` | SES with a verified sender; returns the list of emails sent |
| `postgres_database` | A fresh database loaded with `schema.sql`, `functions.sql`, `views.sql`, `db_users.sql` and `seed.sql` (once per run) |
| `admin` | A superuser connection for checking results; removes test rows afterwards |
| `as_db_user` | Points the code at the test database as `fintrust_score`, `fintrust_query` or `fintrust_report` |
| `clean_config` (automatic) | Clears settings and the config cache before every test, so tests cannot affect each other |

## What is tested, file by file

| File | Tests | Covers |
|---|---|---|
| `unit/test_validators.py` | 93 | Every validation rule, row by row; redaction of personal information; account masking |
| `unit/test_rules.py` | 26 | The three risk rules, their thresholds and the LOW / MEDIUM / HIGH bands |
| `unit/test_ingest.py` | 44 | File handling, quarantine, queue batches, journey events, retries |
| `unit/test_score.py` | 21 | Batch handling: partial failures, per-account ordering, redelivery |
| `unit/test_alert.py` | 24 | Email content, masking, one email per transaction, SES failure |
| `unit/test_query.py`, `test_query_write.py`, `test_query_cors_accounts.py` | 120 | Every route: validation, status codes, error shape, SQL injection attempts |
| `unit/test_db.py` | 23 | Connection reuse; rollback on every error path |
| `unit/test_dynamo.py` | 29 | Keys, journey order, pagination, error wrapping |
| `unit/test_config.py`, `test_log.py`, `test_models.py`, `test_metrics.py` | 72 | Settings, JSON logs, data classes, metric lines |
| `unit/test_report.py` | 36 | Which month to report on, CSV and JSON output, failures |
| `unit/test_failure_paths.py` | 9 | Failures at awkward moments, such as an audit write failing after the real work succeeded |
| `unit/test_iam_policies.py` | 97 | No wildcard actions; every role limited to its own resources |
| `unit/test_storage_config.py`, `test_monitoring_config.py` | 45 | Lifecycle rules match the retention policy; alarms and dashboard use real metric names; Free Tier limits |
| `unit/test_frontend.py` | 31 | The dashboard's files: no key or mock data, no HTML built from data, strict Content-Security-Policy, private hosting, every file copied and uploaded, the Overview figures layout |
| `unit/test_query_trends.py` | 25 | The chart routes: day, week and month bars (Monday weeks, month and year roll-over), validation, the 30-day burst flag, the balance's order, summary and limit |
| `unit/test_demo_tools.py` | 25 | The demonstration file scores exactly as the end-to-end test expects; onboarding is safe; the diagrams match their source |
| `unit/test_repo_hygiene.py` | 22 | No keys or passwords committed; dependencies pinned; clients at module level; scripts valid |
| `integration/test_schema.py` | 46 | Every constraint, index and foreign key rejects bad data |
| `integration/test_erd_matches_schema.py` | 7 | The ERD document matches the deployed tables exactly |
| `integration/test_db_integration.py` | 12 | Rollback and reconnect against a real database |
| `integration/test_score_integration.py` | 18 | Scores, velocity window, atomic writes, break and recover |
| `integration/test_query_integration.py`, `test_write_api_integration.py` | 54 | API SQL against seed data; `flag_account`; database permissions |
| `integration/test_sql_analytics.py` | 24 | Running balance and risk report checked against plain-Python recalculation; window boundaries; views; permissions |
| `integration/test_report_integration.py` | 9 | The report Lambda end to end on seed data: totals, balances chaining month to month, no customer details |
| `integration/test_onboard_integration.py` | 3 | Onboarding an account against a real database |
| `integration/test_pipeline.py` | 7 | The whole pipeline in one process, including the dead-letter queue |
| `integration/test_trends_integration.py` | 23 | The chart data reconciled with `/insights`, with `risk_report.sql` account by account, with `monthly_summary.sql` and the hand-worked balances; a period keeps the balance carried in |

### The pipeline test and the dead-letter queue

`integration/test_pipeline.py` runs the four real handlers together: a CSV goes into S3, and the test checks the quarantine bucket, the ledger, the email, the queues and the API responses. The queue is created with the same redrive policy as the deployed one, so SQS itself (in moto) moves a message to the dead-letter queue after three failed receives.

The dead-letter tests prove four things:

1. A transaction for an account that does not exist yet fails exactly three times, then is in the dead-letter queue, not lost.
2. A different account's transaction in the same file is processed normally.
3. After the account is onboarded and the queue is redriven, the transaction is scored.
4. Two messages for the failing account keep their order.

The same scenario is run on the real account by the end-to-end test.

## The end-to-end test (`scripts/e2e_test.py`)

One script, run against the real deployed system. It uploads a new 12-row file through the public API and checks every stage: the raw bucket, the processed and quarantine buckets, the ledger, the event store, the alert email, the API, the monthly report, and finally failure and recovery through the dead-letter queue. It makes 30 checks and takes about 9 minutes, most of it waiting for the three retries and for the queue's 5-minute duplicate window.

```bash
python scripts/e2e_test.py | tee docs/evidence/w14d5-e2e-output.txt
```

The file it sends is built by `scripts/make_demo_file.py`, the same file used for the live demonstration, and a unit test checks that every row scores exactly what the end-to-end test expects.

## Continuous integration (`.github/workflows/ci.yml`)

| Job | Runs on | Steps |
|---|---|---|
| **Lint and unit tests** | Every push to any branch, and every pull request into `develop` or `main` | `ruff check`; syntax check of every shell script; unit tests; fails below 90% coverage |
| **Integration tests** | Pushes to `develop` only, and only if the first job passed | Starts PostgreSQL 16; integration tests; then the whole suite, failing below 95% coverage; writes a coverage table to the run summary |

Why integration is limited to `develop`: feature branches get feedback in about a minute from the unit job, and the heavier database job runs once, at the point where work is merged. A pull request cannot be merged with a red check.

## How to run the tests yourself

```bash
pytest tests/unit                      # fast, needs nothing
pytest -m "not integration"            # the same, chosen by marker

docker compose up -d                   # start a local PostgreSQL
PGHOST=localhost PGPORT=5433 PGUSER=postgres PGPASSWORD=local-test-only pytest
docker compose down

pytest tests/unit/test_rules.py -v     # one file, with test names
pytest -k dead_letter                  # only tests whose name contains "dead_letter"
pytest --cov=fintrust --cov-report=term-missing   # show any lines no test runs
```

Without a database the integration tests are skipped, not failed. In CI, `REQUIRE_POSTGRES=1` turns a missing database into a failure, so the job cannot pass by silently skipping.

## What is deliberately not automated

- **Tests against the real AWS account in CI.** That would need AWS credentials stored in GitHub and would spend credits on every push. The verify scripts are run by hand after each deployment and their output is saved in `docs/evidence/`.
- **The two unreachable lines** (`validators.py` 179 to 180): a second safety check behind a pattern that already rejects the same input. They are kept as a guard and account for the missing 1%.
