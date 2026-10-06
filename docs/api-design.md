# API Design

**Brief slot:** Week 13 Day 1: Design Kickoff (specification). Read routes are built in Week 13 Day 5, write routes in Week 14 Day 1.
**Status:** draft specification. Response examples are replaced with real output once deployed.

## Overview

| Item | Value |
|---|---|
| Type | Amazon API Gateway **REST API**, regional endpoint, stage `dev` |
| Base URL | `https://{api-id}.execute-api.eu-north-1.amazonaws.com/dev` |
| Backend | One Lambda (`fintrust-capstone-query`) using Lambda proxy integration |
| Transport | HTTPS only |
| Format | JSON, except the body of `POST /upload`, which is CSV text |

## Authentication

Every route requires an API key in the `x-api-key` header. The key belongs to a **usage plan** that limits callers to 10 requests per second (burst 20) and 5,000 requests per day.

- No key or a wrong key: API Gateway itself answers `403 {"message": "Forbidden"}`. The Lambda is never called.
- Over the limit: `429 {"message": "Too Many Requests"}`.

The key is never stored in the repository or in the dashboard's code. The dashboard asks the user for it and keeps it only for the browser session.

## Routes

| Method | Route | Purpose | Success |
|---|---|---|---|
| GET | `/health` | Is the API up and can it reach the database? | 200 |
| GET | `/transactions` | List transactions, filtered and paginated | 200 |
| GET | `/transactions/{transaction_id}/journey` | Timeline and score breakdown for one transaction | 200 |
| GET | `/insights` | Risk summary for the compliance team | 200 |
| GET | `/alerts` | List alerts | 200 |
| POST | `/upload` | Submit a CSV file for processing | 202 |
| POST | `/flag` | Flag an account for review | 201 |
| POST | `/resolve` | Mark an alert as resolved | 200 |

### GET /health

No parameters.

```json
{ "status": "ok", "database": "ok", "version": "0.1.0" }
```

Returns `503` with `"database": "unavailable"` if RDS cannot be reached (for example, the instance is stopped).

### GET /transactions

| Query parameter | Required | Rule |
|---|---|---|
| `account_id` | no | Exact match |
| `risk_level` | no | `LOW`, `MEDIUM` or `HIGH` |
| `from`, `to` | no | Dates, `YYYY-MM-DD`, inclusive |
| `limit` | no | 1 to 100, default 25 |
| `offset` | no | 0 or more, default 0 |

```json
{
  "items": [
    {
      "transaction_id": "TXN-20260915-000123",
      "account_id": "ACC-1007",
      "tx_date": "2026-09-15T09:42:11+02:00",
      "amount": "75000.00",
      "currency": "ZAR",
      "tx_type": "DEBIT",
      "channel": "ONLINE",
      "country_code": "GB",
      "risk_score": 70,
      "risk_level": "HIGH"
    }
  ],
  "pagination": { "limit": 25, "offset": 0, "total": 214, "has_more": true }
}
```

Amounts are returned as text so no precision is lost.

### GET /transactions/{transaction_id}/journey

Read from the DynamoDB event store.

```json
{
  "transaction_id": "TXN-20260915-000123",
  "account_id": "ACC-1007",
  "score": {
    "risk_score": 70,
    "risk_level": "HIGH",
    "rules": [
      { "rule": "HIGH_AMOUNT", "points": 50, "detail": "75000.00 is above 50000" },
      { "rule": "CROSS_BORDER", "points": 20, "detail": "country GB is not ZA" }
    ]
  },
  "events": [
    { "stage": "RECEIVED", "at": "2026-09-15T07:42:15Z", "detail": "uploads/2026-09-15/batch.csv row 14" },
    { "stage": "VALIDATED", "at": "2026-09-15T07:42:15Z" },
    { "stage": "QUEUED", "at": "2026-09-15T07:42:16Z" },
    { "stage": "SCORED", "at": "2026-09-15T07:42:18Z" },
    { "stage": "ALERT_SENT", "at": "2026-09-15T07:42:20Z" }
  ]
}
```

`404` if the transaction id is unknown.

### GET /insights

Optional `from` and `to` dates.

```json
{
  "period": { "from": "2026-07-01", "to": "2026-09-30" },
  "totals": { "transactions": 214, "total_value": "4182930.55", "accounts": 12 },
  "by_risk_level": { "LOW": 171, "MEDIUM": 29, "HIGH": 14 },
  "rules_fired": { "HIGH_AMOUNT": 21, "HIGH_VELOCITY": 6, "CROSS_BORDER": 33 },
  "open_alerts": 5,
  "top_risk_accounts": [
    { "account_id": "ACC-1007", "high_risk_transactions": 4, "status": "FLAGGED" }
  ]
}
```

### GET /alerts

| Query parameter | Required | Rule |
|---|---|---|
| `status` | no | `OPEN` or `RESOLVED` |
| `limit`, `offset` | no | As for `/transactions` |

Returns `items` and `pagination` in the same shape as `/transactions`.

### POST /upload

Accepts the file and returns immediately. Processing continues in the background: the file is stored in the raw bucket, and S3 invokes the ingest Lambda asynchronously.

| Part | Rule |
|---|---|
| Header | `Content-Type: text/csv` |
| Query | `filename` (optional; letters, digits, `-`, `_`, `.` only) |
| Body | CSV text with a header row, at most 5 MB |

```json
{
  "status": "ACCEPTED",
  "upload_id": "0f4c2d8e-6f1b-4b1c-9d0f-5f6d0f1a2b3c",
  "object_key": "uploads/2026-10-06/0f4c2d8e-batch.csv",
  "message": "File accepted. Transactions will appear as they are processed."
}
```

Status `202 Accepted`. `400` for an empty body or a missing header row, `413` if larger than 5 MB.

### POST /flag

```json
{ "account_id": "ACC-1007", "reason": "Four high-risk transactions in 30 days" }
```

Calls the `flag_account` database function, which inserts a `risk_events` row and sets the account status to `FLAGGED` in one atomic step.

```json
{ "risk_event_id": 381, "account_id": "ACC-1007", "account_status": "FLAGGED" }
```

Status `201`. `400` if a field is missing or `reason` is longer than 500 characters, `404` if the account does not exist.

### POST /resolve

```json
{ "alert_id": 57, "resolved_by": "n.hlongwane", "resolution_note": "Confirmed with customer" }
```

```json
{ "alert_id": 57, "status": "RESOLVED", "resolved_at": "2026-10-06T11:05:00Z" }
```

Status `200`. `404` if the alert does not exist, `409` if it is already resolved.

## Errors

Every error produced by the Lambda has the same shape:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "limit must be between 1 and 100",
    "request_id": "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"
  }
}
```

| Status | Code | When |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Bad parameter or body |
| 404 | `NOT_FOUND` | Unknown route, transaction, account or alert |
| 409 | `CONFLICT` | Alert already resolved |
| 413 | `PAYLOAD_TOO_LARGE` | Upload above 5 MB |
| 500 | `INTERNAL_ERROR` | Unexpected failure. The database transaction is rolled back, the detail is logged with the `request_id`, and no internal detail is returned to the caller |
| 503 | `SERVICE_UNAVAILABLE` | Database unreachable |

## Cross-origin access (CORS)

The API allows browser calls only from the dashboard's CloudFront address, with headers `content-type` and `x-api-key`.
