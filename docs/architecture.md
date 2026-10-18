# Architecture

**Brief slot:** Week 13 Day 1: Design Kickoff and Infrastructure Setup · **Status:** as deployed, including sign-in (Week 15 Day 2).

## Diagrams

All four diagrams are in `docs/diagrams/`, each as an editable draw.io file, an SVG (shown below on GitHub) and a PDF (the deliverable). They are built from one description each in `scripts/build_diagrams.py` and drawn by draw.io with the official AWS Architecture Icons; tests fail the build if a picture is not exported from its current source.

| Diagram | Shows | Files |
|---|---|---|
| AWS architecture | Every service, the VPC and its private subnets, the gateway endpoints, the numbered path of one transaction, the failure paths | [PDF](diagrams/architecture.pdf) · [draw.io](diagrams/architecture.drawio) |
| Entity relationship diagram | The five ledger tables in UML class notation, and the DynamoDB key design | [PDF](diagrams/erd.pdf) · [draw.io](diagrams/erd.drawio) · [erd.md](erd.md) |
| Transaction pipeline (UML sequence) | One transaction from upload to alert, and the dead-letter recovery | [PDF](diagrams/pipeline-sequence.pdf) · [draw.io](diagrams/pipeline-sequence.drawio) |
| Sign-in (UML sequence) | Sign-in with Cognito, the session check on every request, sign-out | [PDF](diagrams/sign-in-sequence.pdf) · [draw.io](diagrams/sign-in-sequence.drawio) |

![FinTrust AWS architecture](diagrams/architecture.svg)

![One transaction from upload to alert](diagrams/pipeline-sequence.svg)

The same flow as a text diagram (renders on GitHub without opening a file):

```mermaid
flowchart LR
    U[Compliance staff<br/>browser] -->|password + MFA code| COG[Amazon Cognito<br/>user pool]
    U -->|HTTPS| CF[CloudFront]
    CF -->|origin access control| WEB[(S3 dashboard bucket<br/>private)]
    U -->|x-api-key + ID token| APIGW[API Gateway<br/>REST API + usage plan]
    APIGW -.Cognito authorizer checks the token.-> COG
    APIGW --> Q[query Lambda<br/>in VPC]
    Q -->|session and role| SES_T[(DynamoDB<br/>sessions)]
    Q -->|POST /upload stores CSV| RAW[(S3 raw bucket)]
    RAW -->|ObjectCreated event| ING[ingest Lambda<br/>outside VPC]
    ING -->|invalid rows| QUAR[(S3 quarantine)]
    ING -->|clean CSV| PROC[(S3 processed)]
    ING -.after 2 retries.-> IDLQ[[ingest dead-letter queue]]
    ING -->|one message per transaction<br/>group = account_id| FIFO[[SQS FIFO queue]]
    FIFO -.after 3 failed receives.-> DLQ[[FIFO dead-letter queue]]
    FIFO --> SC[score Lambda<br/>in VPC]
    SC -->|ledger, risk_events, alerts<br/>one DB transaction| RDS[(RDS PostgreSQL<br/>private subnets)]
    SC -->|score + rule breakdown| DDB[(DynamoDB<br/>event store)]
    ING -->|journey events| DDB
    DDB -->|Stream, filter: HIGH only| AL[alert Lambda<br/>outside VPC]
    AL --> SES[SES email to compliance]
    AL -.after 3 retries.-> ADLQ[[alert dead-letter queue]]
    Q --> RDS
    Q --> DDB
    EB[EventBridge monthly cron] --> REP[report Lambda<br/>in VPC]
    REP -->|read only| RDS
    REP -->|CSV + JSON| RPT[(S3 reports bucket)]
```

Security boundaries: everything sits in one AWS account in **eu-north-1 (Stockholm)**. RDS and the three Lambdas that use it (score, query, report) are inside the VPC's private subnets. There is no internet gateway route from those subnets and no NAT Gateway. The only public entry points are CloudFront (the dashboard's files) and API Gateway, which requires an API key **and** a signed-in person on every route except `GET /health`.

## How the pieces connect (plain language)

| Step | What happens | Why it is built this way |
|---|---|---|
| 0. Sign in | A person signs in through **Amazon Cognito** with a password and a code from an authenticator app. The dashboard receives an ID token, and the query Lambda records a session in **DynamoDB**. | Bank staff need a personal identity, not a shared key: every decision is recorded under a name, roles limit who may resolve alerts or flag accounts, and signing out ends access at once. |
| 1. Upload | CSV lands in the **raw** S3 bucket (through `POST /upload` or a direct upload). | S3 is cheap, durable storage, and it can start a Lambda by itself when a file arrives. |
| 2. Ingest | S3 starts the **ingest** Lambda. It validates each row. Bad rows go to the **quarantine** bucket with the reason. Good rows are written to the **processed** bucket and sent to the queue, one message per transaction. | Validation at the door stops bad data reaching the ledger (data quality, POPIA accuracy). |
| 3. Queue | The **SQS FIFO** queue holds messages in order per account. | The queue *decouples* ingest from scoring: if scoring is slow or broken, nothing is lost. FIFO keeps one account's transactions in order, which the velocity rule needs. |
| 4. Score | The **score** Lambda reads from the queue, applies the risk rules, writes the transaction to **RDS** and the score with its rule breakdown to **DynamoDB**. | One place owns scoring and both writes, so the ledger and the event store cannot drift apart. |
| 5. Failure | If a message fails three times it moves to the **dead-letter queue (DLQ)** and an alarm fires. | Nothing is silently dropped. The message waits until a human fixes the cause and redrives it. |
| 6. Alert | DynamoDB Streams passes HIGH scores to the **alert** Lambda, which emails compliance through **SES**. | Alerting is separate from scoring, so an email failure can never block or roll back a ledger write. |
| 7. Read | **API Gateway** checks the API key and the Cognito ID token, then calls the **query** Lambda. It checks that the session is still open and the person's role allows the route, then reads RDS and DynamoDB. | Managed entry point with authentication and throttling, no servers. Checking the role in the Lambda as well as at the gateway means a gateway setting can never open a route by mistake. |
| 8. Report | **EventBridge** runs the **report** Lambda monthly; it runs the CTE report on RDS and writes CSV and JSON to the **reports** bucket. | Scheduled, automatic executive reporting. |

## The network design (the no-NAT constraint)

The brief forbids a NAT Gateway. A Lambda inside a private subnet therefore has **no route to the internet**, so it can only reach AWS services that have a free *gateway endpoint* (S3 and DynamoDB). That single fact decides where each Lambda lives:

| Lambda | In VPC? | Talks to | Why |
|---|---|---|---|
| ingest | No | S3, SQS, DynamoDB | Never touches RDS, and needs SQS (no free endpoint). |
| score | **Yes** | RDS, DynamoDB | Needs RDS. Reaches DynamoDB through the free gateway endpoint. SQS *delivers* to it, so it needs no route to SQS. |
| alert | No | SES, DynamoDB | SES has no free endpoint. Never touches RDS. |
| query | **Yes** | RDS, DynamoDB (events and sessions), S3 | Needs RDS. S3 and DynamoDB through free gateway endpoints. It never calls Cognito: API Gateway checks the token before the Lambda runs, so no network route to Cognito is needed. |
| report | **Yes** | RDS, S3 | Needs RDS. S3 through the free gateway endpoint. |

Three consequences, each a deliberate decision:

1. **Alert is triggered by a DynamoDB Stream**, not a direct Lambda-to-Lambda call, because the score Lambda cannot reach the Lambda service from inside the VPC.
2. **Database login uses IAM database authentication**, not a stored password. The Lambda signs a 15-minute login token with its own IAM role, which needs no network call. This removes the need to reach Secrets Manager or SSM from inside the VPC, and means no database password exists in any Lambda. (Changed from the earlier default of an SSM SecureString, which the VPC Lambdas could not have reached.)
3. **Configuration:** SSM Parameter Store is the single source of truth. The two non-VPC Lambdas read it at cold start. For the three VPC Lambdas the deploy script copies the same values from SSM into environment variables. `config.py` handles both the same way: environment variable first, then SSM.

## Why each service was chosen (SAA-C03 decision framework)

| Service | Resilience | Security | Performance | Cost |
|---|---|---|---|---|
| **S3** | 11 nines durability; versioning protects against overwrite | KMS encryption, Block Public Access, bucket policy requires TLS | Scales without limits for file drops | 5 GB free; lifecycle rules move old data to cheaper storage |
| **Lambda** | Runs across multiple AZs automatically; retries on failure | One least-privilege role per function | Scales per event, warm containers reuse connections | Pay per request, nothing when idle |
| **SQS FIFO + DLQ** | Messages survive a scoring outage; DLQ keeps failed messages for 14 days | Encrypted with KMS; only ingest may send, only score may receive | Buffers bursts so RDS is not overwhelmed | 1M requests free |
| **RDS PostgreSQL** | Automated backups; Multi-AZ is the documented production upgrade | Private subnets, no public endpoint, security group allows port 5432 from the Lambda security group only, IAM database authentication, encrypted at rest | Indexed relational queries, joins, window functions | db.t3.micro, stopped outside working hours |
| **DynamoDB** | Multi-AZ by default, no maintenance | Encrypted at rest by default, access by IAM, holds no customer details | Single-digit millisecond lookups by account | 25 GB and 25 capacity units free |
| **API Gateway** | Managed, multi-AZ | API key, Cognito authorizer, usage plan throttling, TLS only | Handles bursts without servers | 1M calls free |
| **Cognito** | Managed, multi-AZ | MFA required, passwords never reach the dashboard's code, no self sign-up, two groups as roles | Tokens checked at the gateway, no call per request | Essentials plan: 10 000 monthly users free |
| **SES** | Managed delivery with retries | Verified identities only; role limited to one sender | Asynchronous from scoring | 3,000 messages free |
| **EventBridge** | Managed scheduler with retries | Can invoke only the report Lambda | Not applicable | Free |
| **CloudFront** | Served from many edge locations | HTTPS only; origin access control keeps the bucket private; security headers on every file | Cached close to the user, including South African edge locations | 1 TB and 10 million requests free |
| **CloudWatch** | Alarms detect failure early | Logs carry identifiers, never personal details | Logs Insights queries on JSON fields | Within 10 metrics and 10 alarms |
| **KMS** | Key managed and backed up by AWS | Customer-managed key, key policy, rotation enabled | Transparent | About $1 per month |
| **VPC endpoints (gateway)** | Not a single point of failure | Traffic to S3 and DynamoDB stays on the AWS network | No NAT hop | Free, replaces a $32 per month NAT Gateway |

**Why two databases?** The ledger needs relationships, constraints and transactions that either fully happen or fully roll back, so it is relational (RDS). Scores and journey events are written once and read by a known key (account, then time), so they suit a key-value store (DynamoDB). This is the "purpose-built database" principle.

## Failure handling

| Failure | What happens | How it is recovered |
|---|---|---|
| A CSV row is invalid | Row is written to the quarantine bucket with the reason; valid rows continue | Data steward corrects and re-uploads |
| The ingest Lambda crashes | Lambda retries twice, then sends the event to the ingest DLQ | Re-upload the file or replay the event |
| The score Lambda fails on a message | The database transaction is rolled back; SQS redelivers; after 3 failed receives the message moves to the FIFO DLQ and an alarm fires | Fix the cause, then redrive the DLQ |
| One account's message is stuck | Only that account's message group waits; other accounts keep flowing | Automatic once the message reaches the DLQ |
| The session table cannot be reached | The API refuses the request with 503: it fails closed, never open | Automatic once DynamoDB answers; nothing to recover |
| Cognito is unavailable | People already signed in keep working until their token needs renewing; new sign-ins wait | Managed service; nothing stored by FinTrust is affected |
| SES rejects an email | The ledger, the score and the alert row are already saved; the stream retries the email 3 times, then the record's details go to the alert dead-letter queue | Fix the cause (for example an unverified address); the alert is still listed by `GET /alerts` |
| An Availability Zone fails | Lambda, SQS, DynamoDB, S3 and API Gateway continue in other AZs. RDS single-AZ is the one exposed component | Production design: RDS Multi-AZ with automatic failover (not deployed: outside Free Tier) |

The Availability Zone case is worked through component by component in the [Well-Architected review](well-architected-review.md#3-reliability). The dead-letter recovery is proven on the real account by `scripts/e2e_test.py`.
