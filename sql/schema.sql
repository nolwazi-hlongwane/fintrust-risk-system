-- =============================================================================
-- FinTrust Bank SA: transaction ledger schema
-- Brief slot: Week 13 Day 2 (Data Layer)
--
-- Runs on a clean PostgreSQL instance:   psql -f sql/schema.sql
-- Everything is inside one transaction, so if any statement fails the database
-- is left exactly as it was (all or nothing).
--
-- Design: third normal form (3NF).
--   customers 1 --- many accounts 1 --- many transactions
--   transactions 1 --- many risk_events   (one row per rule that fired)
--   accounts     1 --- many risk_events   (rules, plus manual flags by an officer)
--   transactions 1 --- zero or one alerts (only HIGH risk transactions)
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- customers: the only table holding directly identifying personal information
-- -----------------------------------------------------------------------------
CREATE TABLE customers (
    customer_id   VARCHAR(20)   PRIMARY KEY,
    full_name     VARCHAR(120)  NOT NULL,
    email         VARCHAR(254)  NOT NULL,
    phone_number  VARCHAR(20)   NOT NULL,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT uq_customers_email UNIQUE (email),
    CONSTRAINT ck_customers_id_format CHECK (customer_id ~ '^CUST-[0-9]{4,}$'),
    CONSTRAINT ck_customers_email_format CHECK (email LIKE '%_@_%._%')
);

-- -----------------------------------------------------------------------------
-- accounts: each account belongs to exactly one customer
-- -----------------------------------------------------------------------------
CREATE TABLE accounts (
    account_id    VARCHAR(20)   PRIMARY KEY,
    customer_id   VARCHAR(20)   NOT NULL,
    account_type  VARCHAR(20)   NOT NULL,
    status        VARCHAR(20)   NOT NULL DEFAULT 'ACTIVE',
    opened_date   DATE          NOT NULL,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_accounts_customer
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_accounts_id_format CHECK (account_id ~ '^ACC-[0-9]{4,}$'),
    CONSTRAINT ck_accounts_type CHECK (account_type IN ('CHEQUE', 'SAVINGS', 'CREDIT', 'BUSINESS')),
    CONSTRAINT ck_accounts_status CHECK (status IN ('ACTIVE', 'FLAGGED', 'CLOSED'))
);

-- -----------------------------------------------------------------------------
-- transactions: the ledger. One row per transaction, never updated after insert.
--   amount is always positive; tx_type says whether money came in or went out.
--   NUMERIC, not FLOAT: money must be exact.
--   risk_level is the classification in force when the transaction was scored
--   (the thresholds are configurable, so it cannot be recalculated from
--   risk_score later).
-- -----------------------------------------------------------------------------
CREATE TABLE transactions (
    transaction_id  VARCHAR(40)    PRIMARY KEY,
    account_id      VARCHAR(20)    NOT NULL,
    tx_date         TIMESTAMPTZ    NOT NULL,
    amount          NUMERIC(14,2)  NOT NULL,
    currency        CHAR(3)        NOT NULL DEFAULT 'ZAR',
    tx_type         VARCHAR(10)    NOT NULL,
    channel         VARCHAR(20)    NOT NULL,
    merchant_name   VARCHAR(120),
    country_code    CHAR(2)        NOT NULL,
    description     VARCHAR(255),
    risk_score      SMALLINT       NOT NULL,
    risk_level      VARCHAR(10)    NOT NULL,
    source_file     VARCHAR(255)   NOT NULL,
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT now(),

    CONSTRAINT fk_transactions_account
        FOREIGN KEY (account_id) REFERENCES accounts (account_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_transactions_amount_positive CHECK (amount > 0),
    CONSTRAINT ck_transactions_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_transactions_type CHECK (tx_type IN ('CREDIT', 'DEBIT')),
    CONSTRAINT ck_transactions_channel CHECK (channel IN ('POS', 'ONLINE', 'ATM', 'EFT', 'MOBILE', 'BRANCH')),
    CONSTRAINT ck_transactions_country CHECK (country_code ~ '^[A-Z]{2}$'),
    CONSTRAINT ck_transactions_risk_score CHECK (risk_score BETWEEN 0 AND 100),
    CONSTRAINT ck_transactions_risk_level CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH'))
);

-- Indexes required by the brief: account_id and transaction date.
CREATE INDEX idx_transactions_account_id   ON transactions (account_id);
CREATE INDEX idx_transactions_tx_date      ON transactions (tx_date);
-- The API and the velocity rule both ask "this account, in this time range".
CREATE INDEX idx_transactions_account_date ON transactions (account_id, tx_date);
CREATE INDEX idx_transactions_risk_level   ON transactions (risk_level);

CREATE INDEX idx_accounts_customer_id      ON accounts (customer_id);

-- -----------------------------------------------------------------------------
-- risk_events: why something is considered risky.
--   One row per risk rule that fired on a transaction (transaction_id set), or
--   one row per manual flag on an account (transaction_id empty).
-- -----------------------------------------------------------------------------
CREATE TABLE risk_events (
    risk_event_id   BIGINT        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      VARCHAR(20)   NOT NULL,
    transaction_id  VARCHAR(40),
    event_type      VARCHAR(30)   NOT NULL,
    points          SMALLINT      NOT NULL DEFAULT 0,
    reason          VARCHAR(500)  NOT NULL,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    -- The signed-in compliance officer who flagged the account (Week 15 Day 2).
    -- Empty for rule events, which the scoring Lambda records, and for flags
    -- recorded before sign-in existed.
    flagged_by      VARCHAR(60),

    CONSTRAINT fk_risk_events_account
        FOREIGN KEY (account_id) REFERENCES accounts (account_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_risk_events_transaction
        FOREIGN KEY (transaction_id) REFERENCES transactions (transaction_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_risk_events_type
        CHECK (event_type IN ('HIGH_AMOUNT', 'HIGH_VELOCITY', 'CROSS_BORDER', 'MANUAL_FLAG')),
    CONSTRAINT ck_risk_events_points CHECK (points BETWEEN 0 AND 100),
    CONSTRAINT ck_risk_events_reason_not_blank CHECK (length(btrim(reason)) > 0),
    -- A rule event must name its transaction; a manual flag must not.
    CONSTRAINT ck_risk_events_transaction_presence
        CHECK ((event_type = 'MANUAL_FLAG') = (transaction_id IS NULL)),
    -- Only a manual flag is made by a person, so only a manual flag can name one.
    CONSTRAINT ck_risk_events_flagged_by
        CHECK (flagged_by IS NULL OR event_type = 'MANUAL_FLAG')
);

CREATE INDEX idx_risk_events_account_id     ON risk_events (account_id);
CREATE INDEX idx_risk_events_transaction_id ON risk_events (transaction_id);
-- A rule can fire only once per transaction. This also makes re-processing
-- the same message harmless (the second insert is rejected, not duplicated).
CREATE UNIQUE INDEX uq_risk_events_transaction_rule
    ON risk_events (transaction_id, event_type)
    WHERE transaction_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- alerts: one per HIGH risk transaction, worked by the compliance team.
--   The account and the score are NOT repeated here; they are reached through
--   transaction_id (third normal form).
-- -----------------------------------------------------------------------------
CREATE TABLE alerts (
    alert_id         BIGINT        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id   VARCHAR(40)   NOT NULL,
    status           VARCHAR(10)   NOT NULL DEFAULT 'OPEN',
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    resolved_by      VARCHAR(60),
    resolution_note  VARCHAR(500),

    CONSTRAINT fk_alerts_transaction
        FOREIGN KEY (transaction_id) REFERENCES transactions (transaction_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_alerts_transaction UNIQUE (transaction_id),
    CONSTRAINT ck_alerts_status CHECK (status IN ('OPEN', 'RESOLVED')),
    -- A resolved alert must say when and by whom; an open one must not.
    CONSTRAINT ck_alerts_resolution_complete CHECK (
        (status = 'OPEN'     AND resolved_at IS NULL     AND resolved_by IS NULL)
     OR (status = 'RESOLVED' AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL)
    )
);

CREATE INDEX idx_alerts_status ON alerts (status);

COMMIT;
