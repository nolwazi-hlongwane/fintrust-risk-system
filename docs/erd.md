# Entity Relationship Diagram

**Brief slot:** Week 13 Day 1: Design Kickoff (draft); confirmed in Week 13 Day 2: Data Layer; redrawn in UML notation in Week 15 Day 2.
**Status:** matches `sql/schema.sql` exactly. This is checked automatically: `tests/integration/test_erd_matches_schema.py` reads every table, column, type, length, nullability, key and unique rule from a real PostgreSQL database and fails the build if this diagram differs. The diagram deliverable is [diagrams/erd.pdf](diagrams/erd.pdf), with an editable draw.io source in [diagrams/erd.drawio](diagrams/erd.drawio); both are generated from the diagram source below, so they match it.

![FinTrust ERD in UML notation](diagrams/erd.svg)

## How to read it (UML class notation for a database)

The diagram uses the UML class diagram notation that data modellers use for a relational schema:

| Notation | Meaning |
|---|---|
| A box with `«table»` | One table. The heading is the table name; each line is a column. |
| `name : type` | A column and its PostgreSQL type, with its length: `varchar(20)`, `numeric(14,2)`. |
| `«PK»` | Primary key: the column that identifies each row. |
| `«FK»` | Foreign key: the column points to the primary key of another table. |
| `[0..1]` | The column may be empty (NULL). A column without `[0..1]` is `NOT NULL`. |
| `{unique}` | No two rows may hold the same value. |
| A line between two tables | A relationship, labelled with what it means. |
| `1`, `0..1`, `0..*` at each end | Multiplicity: how many rows at that end can be linked to one row at the other end. `0..*` is "zero or more". |

For example, `customers 1 —— 0..* accounts` reads: one customer owns zero or more accounts, and every account belongs to exactly one customer.

```mermaid
classDiagram
    direction LR
    class customers {
        <<table>>
        «PK» customer_id : varchar(20)
        full_name : varchar(120)
        email : varchar(254) {unique}
        phone_number : varchar(20)
        created_at : timestamptz
    }
    class accounts {
        <<table>>
        «PK» account_id : varchar(20)
        «FK» customer_id : varchar(20)
        account_type : varchar(20)
        status : varchar(20)
        opened_date : date
        created_at : timestamptz
    }
    class transactions {
        <<table>>
        «PK» transaction_id : varchar(40)
        «FK» account_id : varchar(20)
        tx_date : timestamptz
        amount : numeric(14,2)
        currency : char(3)
        tx_type : varchar(10)
        channel : varchar(20)
        merchant_name : varchar(120) [0..1]
        country_code : char(2)
        description : varchar(255) [0..1]
        risk_score : smallint
        risk_level : varchar(10)
        source_file : varchar(255)
        created_at : timestamptz
    }
    class risk_events {
        <<table>>
        «PK» risk_event_id : bigint
        «FK» account_id : varchar(20)
        «FK» transaction_id : varchar(40) [0..1]
        event_type : varchar(30)
        points : smallint
        reason : varchar(500)
        created_at : timestamptz
        flagged_by : varchar(60) [0..1]
    }
    class alerts {
        <<table>>
        «PK» alert_id : bigint
        «FK» transaction_id : varchar(40) {unique}
        status : varchar(10)
        created_at : timestamptz
        resolved_at : timestamptz [0..1]
        resolved_by : varchar(60) [0..1]
        resolution_note : varchar(500) [0..1]
    }
    customers "1" -- "0..*" accounts : owns
    accounts "1" -- "0..*" transactions : holds
    accounts "1" -- "0..*" risk_events : is assessed by
    transactions "0..1" -- "0..*" risk_events : triggers
    transactions "1" -- "0..1" alerts : raises
```

### The relationships, in words

| Relationship | Multiplicity | Enforced by |
|---|---|---|
| A customer **owns** accounts | one customer, zero or more accounts | `accounts.customer_id` NOT NULL, foreign key `fk_accounts_customer` |
| An account **holds** transactions | one account, zero or more transactions | `transactions.account_id` NOT NULL, foreign key `fk_transactions_account` |
| An account **is assessed by** risk events | one account, zero or more risk events | `risk_events.account_id` NOT NULL, foreign key `fk_risk_events_account` |
| A transaction **triggers** risk events | a risk event belongs to **zero or one** transaction | `risk_events.transaction_id` may be empty: a manual flag is about an account, not a transaction (`ck_risk_events_transaction_presence`) |
| A transaction **raises** an alert | one transaction, **zero or one** alert | `UNIQUE (transaction_id)` on `alerts`; only High transactions raise one |

The "zero or one" ends are deliberate and are what the database enforces. The earlier crow's-foot version of this diagram drew the risk event to transaction end as "exactly one", which did not match the nullable `transaction_id`; the UML version corrects it.

## Normalisation notes (3NF)

- **1NF:** every column holds one value. The rules that fired for a transaction are separate rows in `risk_events`, not a list in one column.
- **2NF:** every table has a single-column primary key, so no column can depend on part of a key.
- **3NF:** non-key columns depend only on the key. Customer details live only in `customers`, not repeated on accounts or transactions. `alerts` does not repeat the account or the score, because both are reachable through `transaction_id`.
- **`risk_level` beside `risk_score`:** the level is the classification in force when the transaction was scored. The thresholds are configurable, so the level cannot be derived from the score alone later. It is a recorded fact of the scoring decision, not a derived column.
- **`flagged_by` and `resolved_by`:** the username of the signed-in officer (Week 15 Day 2). They are facts about the event (who made the decision), so they belong on the event row. A username is stored, not a name or email address, so no personal contact details are copied into the ledger.

## Constraints that are not visible in the diagram

| Rule | How the database enforces it |
|---|---|
| Amounts are positive and exact | `NUMERIC(14,2)` with `CHECK (amount > 0)` |
| Only known types, channels, levels and statuses | `CHECK` constraints on each column |
| A transaction must belong to an existing account | Foreign key `fk_transactions_account` |
| A rule fires at most once per transaction | Unique index on (`transaction_id`, `event_type`) |
| One alert per transaction | `UNIQUE (transaction_id)` on `alerts` |
| A resolved alert records who and when | `CHECK` across `status`, `resolved_at`, `resolved_by` |
| Only a manual flag names a person | `CHECK (flagged_by IS NULL OR event_type = 'MANUAL_FLAG')` |
| Ledger rows cannot be orphaned | `ON DELETE RESTRICT` on every foreign key |

Indexes: `transactions(account_id)`, `transactions(tx_date)`, `transactions(account_id, tx_date)`, `transactions(risk_level)`, `accounts(customer_id)`, `risk_events(account_id)`, `risk_events(transaction_id)`, `alerts(status)`.

## Views and the stored function

These are not entities, so they are not in the diagram. They are built on the five tables above.

| Object | Kind | Built on | Purpose |
|---|---|---|---|
| `v_risk_dashboard` | View | `accounts`, `transactions`, `alerts`, `risk_events` | One row per account with its risk picture |
| `v_open_alerts` | View | `alerts`, `transactions`, `accounts`, `risk_events` | The open alert work queue |
| `flag_account(account_id, reason, flagged_by)` | Function | `accounts`, `risk_events` | Flags an account and records why and by whom, atomically |

See [sql-analytics.md](sql-analytics.md).

## Relationship to DynamoDB

RDS is the system of record. DynamoDB holds two tables that are not part of the relational model:

| Table | Keys | Holds |
|---|---|---|
| `fintrust-capstone-events` | partition key `account_id`, sort key `tx_timestamp` (the transaction time, then the transaction id) | A fast-lookup copy of each score with its rule breakdown, and every step of each transaction's journey |
| `fintrust-capstone-sessions` | partition key `session_id` | One record per sign-in: who, which role, when it started, when it was last used, and how it ended |

Neither holds customer details.
