# Final Audit Against the Brief and Rubric

**Brief slot:** Week 14 Day 5: End-to-End Integration and Submission (Portfolio Check-In #14)
**Audited:** 7 October 2026, against the Capstone Project Brief and the Capstone Assessment Rubric. **Re-audited:** Week 15 Day 2, after sign-in, roles and sessions were added and every diagram was redrawn; the changes are marked "Week 15 Day 2" below. Test counts re-checked on Sunday 18 October 2026 (Week 15, presentation preparation) by running the whole suite against PostgreSQL 16.

Each rubric descriptor at the "Exceeds Expectations" level is broken into its parts below, with where the proof is. "Status" says what has been done; items that depend on a step still to be run on the real account are marked honestly.

## C1. Cloud Architecture and AWS Implementation (20 points)

| Exceeds descriptor | Status | Proof |
|---|---|---|
| Full serverless architecture: S3 → ingest Lambda → SQS FIFO → scoring Lambda → RDS + DynamoDB → API Gateway → SES | Deployed and working end to end | `docs/diagrams/architecture.pdf`; `scripts/e2e_test.py`; evidence `w13d5-lambda-functions.png`, `w14d4-lambda-functions.png`, `w13d5-api-gateway-stage.png`, `w14d1-ses-alert-email.png` |
| Five functions: ingest (S3 trigger), score (SQS trigger), alert (SES), query (API), report (EventBridge monthly) | Met | `infrastructure/lambda/deploy_function.sh`; `w14d4-eventbridge-rule.png`, `w14d1-alert-lambda-trigger.png` |
| Every Lambda role is least-privilege; no wildcard actions | Met, and enforced by 98 tests | `docs/security-plan.md` (role matrix); `infrastructure/iam/policies/`; `tests/unit/test_iam_policies.py`; `w13d1-iam-roles.png`, `w13d1-permission-boundary.png` |
| RDS has no public endpoint | Met | `infrastructure/rds/deploy_rds.sh`; `w13d2-rds-private.png`, `w13d2-security-groups.png` |
| DLQ behaviour has been tested | Automated tests, and on the real account by the end-to-end test | `tests/integration/test_pipeline.py`; `scripts/e2e_test.py` stage 8; `w14d3-dead-letter-tests.png`, `w14d5-e2e-output.txt`, `w14d5-dlq-journey.png` |
| Billing evidence confirms Free Tier throughout | Met | `w14d5-billing-summary.png`, `w14d5-billing-credits.png`, `w13d1-billing-alarm.png`; see "Free Tier and cost" below |
| NAT Gateway excluded | Met | `infrastructure/network/deploy_network.sh`; `docs/architecture.md` |
| Survives a simulated AZ failure | Analysed component by component; the dead-letter recovery, which is what protects data during a database outage, is tested live | `docs/well-architected-review.md` section 3 |
| Every service choice justified (resilience, security, performance, cost) | Met | `docs/architecture.md`, "Why each service was chosen" |

## C2. SQL and Data Modelling (15 points)

| Exceeds descriptor | Status | Proof |
|---|---|---|
| `schema.sql`: foreign keys, indexes, NOT NULL constraints | Met | `sql/schema.sql`; 46 tests in `tests/integration/test_schema.py`; `w13d2-schema-executed.png` |
| `seed.sql`: 200+ realistic rows | Met: 275 transactions, 12 accounts, 3 months | `sql/seed.sql`; `scripts/generate_seed.py` |
| `risk_report.sql`: CTE | Met: five CTEs, rolling 30-day window, `PERCENT_RANK` | `sql/risk_report.sql`; `w14d4-risk-report-sql.png` |
| `monthly_summary.sql`: window function, hand-verified | Met | `sql/monthly_summary.sql`; hand verification in `docs/sql-analytics.md`; `w14d4-running-balance-sql.png` |
| `views.sql` | Met: two views | `sql/views.sql`; `w14d4-view-risk-dashboard.png` |
| One stored procedure or function | Met: `flag_account` | `sql/functions.sql`; `w14d1-curl-flag-resolve.png` |
| ERD normalised to 3NF, matching the deployed schema exactly | Met; a test compares the ERD with a real database. Week 15 Day 2: redrawn in UML class notation, with multiplicities that match the nullable columns exactly, and the DynamoDB key design beside it | `docs/diagrams/erd.pdf`, `docs/erd.md`; `tests/integration/test_erd_matches_schema.py`; `tests/unit/test_demo_tools.py` |

## C3. Python Development and Code Quality (15 points)

| Exceeds descriptor | Status | Proof |
|---|---|---|
| All ten modules: config, db, dynamo, models, validators, ingest, score, alert, query, report | Met (plus `log`, `rules`, `metrics` as small helpers) | `fintrust/` |
| Boto3 clients initialised at module level | Met, and checked by a test | `tests/unit/test_repo_hygiene.py` |
| Every exception path rolls back the connection | Met: all database work goes through `db.transaction()`; only `db.py` imports psycopg2 | `fintrust/db.py`; `tests/unit/test_db.py`, `tests/integration/test_db_integration.py` |
| Structured JSON logging throughout | Met | `fintrust/log.py`; `w13d4-logs-insights.png`, `w14d2-logs-insights-failed-ingestion.png` |
| `requirements.txt` pinned | Met, and checked by a test | `requirements.txt`, `requirements-dev.txt` |
| `json.dumps(default=str)` on all RDS responses | Met | `fintrust/query.py` (`respond`), `fintrust/report.py` (`store`) |
| A proper package, not loose scripts | Met | `fintrust/__init__.py`, `pyproject.toml` |

## C4. Testing and CI/CD (10 points)

| Exceeds descriptor | Status | Proof |
|---|---|---|
| Full pytest suite, unit and integration | Met: 1,194 tests (971 unit, 223 integration), 99% coverage of `fintrust/` (889 at submission; 56 added in Week 15 Day 1, 209 in Week 15 Day 2, 40 in Week 15 Day 4) | `tests/`; `docs/testing.md`; `w14d3-pytest-local.png`, `w14d3-coverage.png` |
| moto `@mock_aws` fixtures via `conftest.py` | Met | `tests/conftest.py` |
| GitHub Actions CI green on every push | Met | `.github/workflows/ci.yml`; `w14d3-ci-feature-branch.png`, `w14d3-ci-develop.png` |
| Integration tests scoped to the develop branch only | Met, and checked by a test | `.github/workflows/ci.yml` |
| The end-to-end test script has been run successfully against the real deployed system | Met: 30 of 30 checks in 5 minutes 29 seconds | `scripts/e2e_test.py`; `docs/evidence/w14d5-e2e-output.txt`, `w14d5-e2e-passed.png` |

## C5. Data Governance and Compliance (10 points)

All five elements are in one design document, `docs/data-governance.md`, each specific to this system.

| Required element | Status | Section |
|---|---|---|
| POPIA field classification | Met: every field of `transactions` classified, with storage, access and retention | 1 |
| Data quality and validation framework | Met: the rules, and what happens to failures (quarantine, dead-letter queue, alarm) | 2 |
| Lifecycle policy with named retention periods | Met: hot 12 months, warm 3 years, cold 7 years (FSCA), enforced by lifecycle rules | 3 |
| Migration strategy: correct AWS service and 7 Rs classification, justified | Met: AWS Application Migration Service, Replatform, with why not Rehost or Refactor | 4 |
| Risk register: three risks, each with probability, impact and a named AWS control | Met | 5 |

The one-page "Data lifecycle and POPIA compliance statement" asked for in the brief's checklist is `docs/popia-lifecycle-statement.md`.

## C6. Documentation and Portfolio Completeness (10 points)

The brief's artefact checklist, item by item:

| Checklist item | Where |
|---|---|
| Architecture diagram, draw.io export as PDF, with all services, data flows and security boundaries | `docs/diagrams/architecture.pdf` (source `architecture.drawio`). Week 15 Day 2: official AWS icons and groups (AWS Cloud, Region, VPC, private subnets, gateway endpoints), numbered flow with a key, failure paths. Two UML sequence diagrams added: `pipeline-sequence.pdf` (upload to alert, and dead-letter recovery) and `sign-in-sequence.pdf` |
| ERD diagram normalised to 3NF | `docs/diagrams/erd.pdf` (source `erd.drawio`), `docs/erd.md` |
| API design document: routes, methods, request body, response shape, authentication | `docs/api-design.md` (Week 15 Day 2: API key plus Cognito sign-in, roles, the session routes, sorting); `infrastructure/apigateway/openapi.json` |
| Security plan and IAM role matrix, one row per Lambda | `docs/security-plan.md` |
| Data lifecycle and POPIA compliance statement (1 page) | `docs/popia-lifecycle-statement.md` |
| Risk register | `docs/data-governance.md` section 5 |
| `schema.sql`, `seed.sql`, `risk_report.sql`, `monthly_summary.sql`, `views.sql`, stored function | `sql/` |
| Complete `fintrust/` package with all 10 modules | `fintrust/` |
| `tests/` with `conftest.py` and at least 3 test files | `tests/` (41 test files) |
| `requirements.txt` with pinned versions | `requirements.txt` |
| `.github/workflows/ci.yml` | `.github/workflows/ci.yml` |
| `README.md` a non-technical person can follow | `README.md` |
| Screenshot: API Gateway stage | `w13d5-api-gateway-stage.png` |
| Screenshot: Lambda functions list | `w14d4-lambda-functions.png` |
| Screenshot: CloudWatch dashboard | `w14d2-dashboard.png` |
| Screenshot: RDS instance status | `w13d2-rds-status.png` |
| curl output: GET /transactions and POST /upload | `w13d5-curl-transactions.png`, `w14d1-curl-upload-202.png` |
| GitHub Actions run, all tests passing | `w14d3-ci-develop.png` |
| Logs Insights screenshot with structured JSON output | `w13d4-logs-insights.png`, `w14d2-logs-insights-failed-ingestion.png` |
| Well-Architected review self-assessment (Week 14 Day 5) | `docs/well-architected-review.md` |
| Conventional Commits, meaningful incremental history | The Git history: feature branches merged through pull requests, one phase at a time |

## C7 and C8. Presentation (20 points, marked live in Week 16)

Nothing more is built for these. Preparation material:

| Need | Where |
|---|---|
| A demo that runs end to end without failure: upload → ETL → store → score → dashboard → alert | `docs/demo-runbook.md`; rehearse with `scripts/e2e_test.py` |
| Answers that name parts of my own architecture | Panel preparation notes, one section per build phase |
| Business framing: risk reduction, cost containment, compliance posture | Panel preparation notes, "Business presentation" |

## Where the build differs from the brief's wording, and why

Each of these was a deliberate decision. The reasons are in the document named.

| Brief says | What was built | Why | Recorded in |
|---|---|---|---|
| `ingest.py`: "load RDS and DynamoDB" | Ingest writes journey events to DynamoDB and sends valid rows to the queue; the score Lambda writes RDS | The brief's own architecture diagram and rubric C1 put the ledger write after the queue. Confirmed with the facilitator on 6 October | `docs/build-log.md`, Week 13 Day 4 |
| `POST /upload` using `InvocationType='Event'` | The API stores the file in S3; S3 invokes the ingest Lambda asynchronously | The API Lambda is in the VPC with no NAT Gateway and cannot call the Lambda service. The S3 notification is an asynchronous (Event) invocation, so the behaviour asked for is the same, and there is one ingest path instead of two | `docs/api-design.md` |
| Alert Lambda dispatching SES emails | Triggered by a DynamoDB stream, not called by the score Lambda | Same network constraint; it also means an email failure cannot affect scoring | `docs/architecture.md` |
| Database credentials in SSM or Secrets Manager (Week 6) | IAM database authentication; no password exists | The VPC Lambdas cannot reach SSM or Secrets Manager without a NAT Gateway or paid endpoints. A signed token needs no network call | `docs/security-plan.md` |
| DynamoDB: "session tokens, risk scores, and event log" | All three since Week 15 Day 2: risk scores and the event log in `fintrust-capstone-events`, sign-in sessions in `fintrust-capstone-sessions` | Until Week 15 Day 2 the only authentication was an API key, so there were no sessions to store. Adding sign-in for people made the third job real: one record per sign-in, checked on every request, ended at sign-out | `fintrust/sessions.py`; `docs/security-plan.md` |
| Authentication: "API Gateway with API key authentication" | API key on every route, **and** a Cognito sign-in with MFA on every route except `GET /health` | An API key names an application, not a person. Confidential bank data needs to know who is looking and who decided; the API key and its usage plan are kept exactly as the brief asks | `docs/api-design.md`, "Authentication" |
| ETL output as "Parquet-equivalent" | Cleaned, validated CSV in the processed bucket | Parquet needs a large library layer in Lambda; Parquet is named as the production format in the lifecycle policy | `docs/data-governance.md` |
| Tables: accounts, transactions, risk_events, alerts | Those four plus `customers` | Third normal form and the POPIA classification are clearer with personal contact details in their own table | `docs/erd.md` |
| Pre-signed URLs for browser upload (Week 3 concept) | Upload through `POST /upload`, capped at 5 MB | The brief's Week 14 Day 1 names `POST /upload`. A pre-signed URL is the right upgrade for files above the API's size limit | `docs/api-design.md` |
| `ORDER BY tx_date` in the running balance | `ORDER BY tx_date, transaction_id` | A tie-breaker, so two transactions with the same timestamp always appear in the same order | `sql/monthly_summary.sql` |
| "Within the AWS Free Tier" | The account is on the newer credit-based free plan | RDS, the KMS key and the small tunnel instance draw on credits; nothing is charged. The database is stopped nightly | "Free Tier and cost" below |

## Beyond the brief

Built in addition, without replacing anything required: sign-in for people with Amazon Cognito (MFA required, two roles, sessions that end on sign-out or after 15 idle minutes, every decision recorded under the signed-in username), light and dark appearance, sorting and filtering done by the database (Week 15 Day 2); the web dashboard on CloudFront, with charts drawn from the SQL analytics (Week 15 Day 1: `GET /insights/trends`, `GET /accounts/{account_id}/balance`); the transaction journey and explainable scores; `GET /alerts`, `GET /accounts`, `GET /health`; dead-letter queues for the ingest and alert Lambdas as well as the scoring queue; a permission boundary on every role; three least-privilege database users; alarms on every dead-letter queue; the automatic nightly stop of the database.

## Free Tier and cost

| Service | Free allowance | This project |
|---|---|---|
| Lambda | 1M requests a month | A few thousand |
| API Gateway | 1M calls a month | A few thousand |
| SQS | 1M requests a month | A few thousand |
| DynamoDB | 25 GB, 25 read and 25 write units | Events table: 5 read and 10 write units on the table and on its index. Sessions table: 5 and 5. Total 15 of 25 read, 25 of 25 write; a few MB |
| Cognito | Essentials plan: 10,000 monthly active users free | A handful of staff and one automation user |
| S3 | 5 GB | A few MB |
| SES | 3,000 messages a month (sandbox) | Tens |
| CloudWatch | 10 custom metrics, 10 alarms, 3 dashboards | 9 metrics, 8 alarms, 1 dashboard |
| CloudFront | 1 TB and 10M requests a month | A few MB |
| EventBridge | Scheduled rules free | 1 rule, 2 schedules |
| NAT Gateway | Not free | Not used |
| RDS db.t3.micro | 750 hours a month on the legacy plan | On this account's credit plan it draws credits while running, so it is stopped outside working sessions and automatically at 20:00 |
| KMS customer-managed key | Not free | About 1 USD a month from credits |

Evidence: `w14d5-billing-summary.png` (charges for the month), `w14d5-billing-credits.png` (credits applied and remaining), `w13d1-billing-alarm.png` (the 11 USD, about R200, alarm).

## Submission steps

1. `scripts/e2e_test.py` run on the real account: 30 of 30 checks passed (`docs/evidence/w14d5-e2e-output.txt`).
2. Week 14 Day 5 screenshots and the two billing screenshots captured in `docs/evidence/`.
3. `develop` merged into `main`; repository shared with the facilitator.
4. Repository link submitted (Portfolio Check-In #14).
