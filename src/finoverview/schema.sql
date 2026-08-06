-- finance-overview schema
-- Money is stored as INTEGER minor units (cents). Never floats for money.
-- Snapshot tables are append-only: never UPDATE or DELETE a snapshot row.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id                   INTEGER PRIMARY KEY,
    provider             TEXT    NOT NULL,           -- 'saxo' | 'enablebanking' | 'manual'
    external_id          TEXT    NOT NULL,           -- provider's own account identifier
    institution          TEXT,                       -- 'Rabobank' | 'KBC' | 'Saxo'
    iban                 TEXT,                       -- own IBAN, when the provider gives one
    label                TEXT    NOT NULL,
    kind                 TEXT    NOT NULL,           -- checking|savings|brokerage|property|vehicle|other
    currency             TEXT    NOT NULL,
    liquid               INTEGER NOT NULL DEFAULT 1, -- 0 for illiquid assets (property, etc.)
    encumbered           INTEGER NOT NULL DEFAULT 0, -- 1 = pledged as collateral, not freely available
    include_in_networth  INTEGER NOT NULL DEFAULT 1,
    tx_fetched_at        TEXT,                       -- last transaction fetch ATTEMPT
    details_fetched_at   TEXT,                       -- last /details call; caches the call, not the IBAN
    tx_backfilled_at     TEXT,                       -- when a deep history walk last COMPLETED
    created_at           TEXT    NOT NULL,
    UNIQUE (provider, external_id)
);

CREATE TABLE IF NOT EXISTS collector_runs (
    id           INTEGER PRIMARY KEY,
    collector    TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    status       TEXT    NOT NULL,                   -- running|ok|error
    rows_written INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_collector ON collector_runs (collector, started_at DESC);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts (id),
    ts            TEXT    NOT NULL,                  -- ISO8601 UTC, when we collected it
    as_of         TEXT,                              -- the provider's own reference timestamp
    balance_minor INTEGER NOT NULL,
    currency      TEXT    NOT NULL,
    balance_type  TEXT    NOT NULL DEFAULT 'default',-- provider-specific: expected|closingBooked|...
    run_id        INTEGER REFERENCES collector_runs (id),
    UNIQUE (account_id, ts, balance_type)
);
CREATE INDEX IF NOT EXISTS idx_bal_account_ts ON balance_snapshots (account_id, ts DESC);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts (id),
    ts                  TEXT    NOT NULL,
    instrument_id       TEXT    NOT NULL,            -- Saxo Uic, or ISIN for manual holdings
    symbol              TEXT,
    isin                TEXT,
    name                TEXT,
    asset_class         TEXT,                        -- equity|etf|bond|fund|crypto|cash|fx|other
    quantity            REAL    NOT NULL,
    avg_open_price      REAL,
    last_price          REAL,
    market_value_minor  INTEGER NOT NULL,
    currency            TEXT    NOT NULL,
    unrealized_pl_minor INTEGER,
    exchange            TEXT,
    country             TEXT,
    run_id              INTEGER REFERENCES collector_runs (id),
    UNIQUE (account_id, ts, instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_pos_ts ON position_snapshots (ts DESC);

-- External money moving into or out of the portfolio. Required for TWR/MWR.
-- Sign convention: positive = money in, negative = money out.
CREATE TABLE IF NOT EXISTS portfolio_cash_flows (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts (id),
    ts           TEXT    NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency     TEXT    NOT NULL,
    kind         TEXT    NOT NULL,                   -- deposit|withdrawal|dividend|fee|tax|interest
    external_id  TEXT,                               -- provider id, for idempotent upsert
    note         TEXT,
    UNIQUE (account_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_flow_ts ON portfolio_cash_flows (ts);

-- Bank transactions from Enable Banking. Not a snapshot table: the same
-- transaction is re-fetched on every overlapping window, so this one is an
-- idempotent upsert on (account_id, external_id) rather than append-only.
--
-- Only BOOKED transactions land here. Pending ones mutate (amount and even
-- counterparty change on settlement) and would churn rows for no benefit.
CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts (id),
    external_id       TEXT    NOT NULL,   -- bank's entry reference, or a synthetic digest
    booking_date      TEXT    NOT NULL,   -- YYYY-MM-DD
    value_date        TEXT,
    amount_minor      INTEGER NOT NULL,   -- signed: negative = money left the account
    currency          TEXT    NOT NULL,
    counterparty      TEXT,               -- creditor/debtor name, as the bank spells it
    counterparty_iban TEXT,
    payee_key         TEXT,               -- IBAN if known, else folded name; the grouping key
    description       TEXT,
    bank_code         TEXT,               -- bank_transaction_code, kept for later triage
    run_id            INTEGER REFERENCES collector_runs (id),
    UNIQUE (account_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions (booking_date DESC);
CREATE INDEX IF NOT EXISTS idx_tx_payee ON transactions (payee_key, booking_date DESC);
CREATE INDEX IF NOT EXISTS idx_tx_account_date ON transactions (account_id, booking_date DESC);

CREATE TABLE IF NOT EXISTS fx_rates (
    id     INTEGER PRIMARY KEY,
    as_of  TEXT NOT NULL,                            -- YYYY-MM-DD
    base   TEXT NOT NULL,
    quote  TEXT NOT NULL,
    rate   REAL NOT NULL,                            -- 1 base = <rate> quote
    UNIQUE (as_of, base, quote)
);
CREATE INDEX IF NOT EXISTS idx_fx_lookup ON fx_rates (base, quote, as_of DESC);

-- Recurring costs and incomes, mirrored from config/assets.toml
CREATE TABLE IF NOT EXISTS recurring (
    id           INTEGER PRIMARY KEY,
    key          TEXT    NOT NULL UNIQUE,
    label        TEXT    NOT NULL,
    kind         TEXT    NOT NULL,                   -- income|cost
    amount_minor INTEGER NOT NULL,
    currency     TEXT    NOT NULL,
    period       TEXT    NOT NULL,                   -- monthly|quarterly|yearly
    category     TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT    NOT NULL
);

-- IBANs that are mine, declared in config/assets.toml as [[own_iban]] blocks and
-- mirrored here by the manual collector. This is the answer to "which accounts
-- form my own asset network" for accounts that no collector can tell us about:
-- Saxo's API never returns an IBAN, and a bank you have linked can still be the
-- destination of a transfer months before its own consent is granted.
--
-- Stored normalized (upper-case, no separators) so a match never depends on how
-- one bank chose to space out the counterparty field.
CREATE TABLE IF NOT EXISTS own_ibans (
    iban       TEXT PRIMARY KEY,
    label      TEXT,
    updated_at TEXT NOT NULL
);

-- Enable Banking sessions. valid_until is the PSD2 consent expiry you must renew.
CREATE TABLE IF NOT EXISTS provider_sessions (
    id           INTEGER PRIMARY KEY,
    provider     TEXT NOT NULL,
    institution  TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    valid_until  TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (provider, institution)
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider           TEXT PRIMARY KEY,
    access_token       TEXT,
    refresh_token      TEXT,
    access_expires_at  TEXT,
    refresh_expires_at TEXT,
    updated_at         TEXT NOT NULL
);

-- Views are DROPped before being CREATEd, unlike the tables above. A view holds no
-- data, so recreating it on every init is free — and `CREATE VIEW IF NOT EXISTS`
-- would silently leave an old definition in place on a database that already
-- exists, which is exactly when a fix to one of these needs to land.
DROP VIEW IF EXISTS latest_balances;
DROP VIEW IF EXISTS latest_positions;
DROP VIEW IF EXISTS expenses;
DROP VIEW IF EXISTS internal_transfers;
DROP VIEW IF EXISTS external_transactions;
DROP VIEW IF EXISTS own_iban_registry;
DROP VIEW IF EXISTS collector_health;

-- Most recent balance per account.
CREATE VIEW latest_balances AS
SELECT b.*
FROM balance_snapshots b
JOIN (
    SELECT account_id, MAX(ts) AS ts
    FROM balance_snapshots
    GROUP BY account_id
) m ON m.account_id = b.account_id AND m.ts = b.ts;

-- Most recent position set per account.
CREATE VIEW latest_positions AS
SELECT p.*
FROM position_snapshots p
JOIN (
    SELECT account_id, MAX(ts) AS ts
    FROM position_snapshots
    GROUP BY account_id
) m ON m.account_id = p.account_id AND m.ts = p.ts;

-- Every IBAN that belongs to me, from both sources: the ones collectors learned
-- on their own (accounts.iban) and the ones declared by hand (own_ibans).
-- Normalized on the way out so both sides of the comparison below are folded the
-- same way — banks are inconsistent about spacing IBANs, and an unfolded compare
-- turns "BE54 0636 2680 0897" into a stranger you appear to pay every month.
CREATE VIEW own_iban_registry AS
SELECT iban FROM own_ibans WHERE iban <> ''
UNION
SELECT UPPER(REPLACE(REPLACE(REPLACE(iban, ' ', ''), '-', ''), '.', ''))
FROM accounts
WHERE iban IS NOT NULL AND TRIM(iban) <> '';

-- Transactions with transfers between your own accounts removed, both directions.
-- A KBC -> Rabobank sweep is not an expense, and counting it would roughly double
-- the monthly figure — the same move is booked twice, once as spend on the account
-- it left and once as income on the account it landed in. Neither side survives
-- here: only money entering or leaving the network as a whole is real.
CREATE VIEW external_transactions AS
SELECT t.*, a.label AS account_label, a.institution
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE t.counterparty_iban IS NULL
   OR UPPER(REPLACE(REPLACE(REPLACE(t.counterparty_iban, ' ', ''), '-', ''), '.', ''))
      NOT IN (SELECT iban FROM own_iban_registry);

-- The complement: money shuffled inside the network. Not spend, but worth being
-- able to see — a neutralization you can't inspect is indistinguishable from a
-- collector that silently dropped the rows.
CREATE VIEW internal_transfers AS
SELECT t.*, a.label AS account_label, a.institution
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE t.counterparty_iban IS NOT NULL
  AND UPPER(REPLACE(REPLACE(REPLACE(t.counterparty_iban, ' ', ''), '-', ''), '.', ''))
      IN (SELECT iban FROM own_iban_registry);

-- Money that actually left the household.
CREATE VIEW expenses AS
SELECT * FROM external_transactions WHERE amount_minor < 0;

-- Freshness per collector: the dashboard's staleness indicator reads this.
CREATE VIEW collector_health AS
SELECT r.collector,
       r.started_at,
       r.finished_at,
       r.status,
       r.rows_written,
       r.error
FROM collector_runs r
JOIN (
    SELECT collector, MAX(id) AS id
    FROM collector_runs
    GROUP BY collector
) m ON m.id = r.id;
