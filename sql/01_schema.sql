-- ============================================================================
-- NAV CALCULATION ENGINE  --  relational schema (SQLite)
-- ----------------------------------------------------------------------------
-- Design notes
--   * Base currency is EUR. Securities are priced in their own currency and
--     converted through fx_rates.
--   * Reference data, the things that rarely change, lives in security_master
--     and share_classes. Facts dated to a particular day live in their own
--     tables and point back by id. Each fact is stored once and joined at query
--     time rather than copied around.
--   * SQLite has five storage classes: INTEGER, REAL, TEXT, BLOB, NULL. Dates
--     are TEXT in ISO 'YYYY-MM-DD' form, which sorts chronologically as text.
--     Money is REAL here because it keeps the code readable, but a production
--     system would use fixed point or decimal so rounding cannot move the NAV.
-- ============================================================================

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS exceptions;
DROP TABLE IF EXISTS nav_daily;
DROP TABLE IF EXISTS subscriptions_redemptions;
DROP TABLE IF EXISTS fee_accruals;
DROP TABLE IF EXISTS corporate_actions;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS holdings;
DROP TABLE IF EXISTS fx_rates;
DROP TABLE IF EXISTS daily_prices;
DROP TABLE IF EXISTS share_classes;
DROP TABLE IF EXISTS security_master;

-- ---- Reference data --------------------------------------------------------

CREATE TABLE security_master (
    security_id  INTEGER PRIMARY KEY,
    isin         TEXT NOT NULL UNIQUE,
    ticker       TEXT NOT NULL,
    name         TEXT NOT NULL,
    asset_class  TEXT NOT NULL,          -- 'EQUITY' | 'BOND'
    currency     TEXT NOT NULL,          -- quote currency of the instrument
    exchange     TEXT
);

CREATE TABLE share_classes (
    share_class_id       INTEGER PRIMARY KEY,
    class_name           TEXT NOT NULL,   -- e.g. 'EUR Acc'
    currency             TEXT NOT NULL,   -- currency the class is struck in
    distribution_policy  TEXT NOT NULL,   -- 'ACC' | 'DIST'
    mgmt_fee_bps         INTEGER NOT NULL,-- annual management fee, basis points
    inception_date       TEXT NOT NULL
);

-- ---- Time-series market data -----------------------------------------------

CREATE TABLE daily_prices (
    price_date   TEXT    NOT NULL,        -- 'YYYY-MM-DD'
    security_id  INTEGER NOT NULL,
    close_price  REAL    NOT NULL,        -- in the security's own currency
    PRIMARY KEY (price_date, security_id),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE fx_rates (
    rate_date    TEXT NOT NULL,           -- 'YYYY-MM-DD'
    currency     TEXT NOT NULL,           -- e.g. 'USD'
    rate_to_eur  REAL NOT NULL,           -- 1 unit of currency = rate_to_eur EUR
    PRIMARY KEY (rate_date, currency)
);

-- ---- Portfolio -------------------------------------------------------------

CREATE TABLE holdings (
    position_date  TEXT    NOT NULL,      -- fund-level position on this date
    security_id    INTEGER NOT NULL,
    quantity       REAL    NOT NULL,      -- units held (split-adjusted)
    PRIMARY KEY (position_date, security_id),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE transactions (
    txn_id       INTEGER PRIMARY KEY,
    trade_date   TEXT    NOT NULL,
    security_id  INTEGER NOT NULL,
    txn_type     TEXT    NOT NULL,        -- 'BUY' | 'SELL'
    quantity     REAL    NOT NULL,
    price        REAL    NOT NULL,        -- trade price in security currency
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

CREATE TABLE corporate_actions (
    ca_id             INTEGER PRIMARY KEY,
    security_id       INTEGER NOT NULL,
    ca_type           TEXT    NOT NULL,   -- 'DIVIDEND' | 'SPLIT'
    ex_date           TEXT    NOT NULL,
    pay_date          TEXT,
    amount_per_share  REAL,               -- for DIVIDEND (security currency)
    split_ratio       REAL,               -- for SPLIT (e.g. 2.0 = 2-for-1)
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);

-- ---- Investor dealing ------------------------------------------------------

CREATE TABLE subscriptions_redemptions (
    flow_id         INTEGER PRIMARY KEY,
    flow_date       TEXT    NOT NULL,
    share_class_id  INTEGER NOT NULL,
    flow_type       TEXT    NOT NULL,     -- 'SUB' | 'RED'
    shares          REAL    NOT NULL,     -- number of shares dealt (always > 0)
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);

CREATE TABLE fee_accruals (
    accrual_date    TEXT    NOT NULL,
    share_class_id  INTEGER NOT NULL,
    fee_amount      REAL    NOT NULL,     -- daily management fee accrued (EUR)
    PRIMARY KEY (accrual_date, share_class_id),
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);

-- ---- Engine output ---------------------------------------------------------

CREATE TABLE nav_daily (
    nav_date            TEXT    NOT NULL,
    share_class_id      INTEGER NOT NULL,
    net_assets_eur      REAL    NOT NULL, -- class net assets in base ccy
    shares_outstanding  REAL    NOT NULL,
    nav_per_share_eur   REAL    NOT NULL, -- struck (pre-deal) NAV per share, EUR
    nav_per_share_ccy   REAL    NOT NULL, -- NAV per share in the class currency
    PRIMARY KEY (nav_date, share_class_id),
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);

CREATE TABLE exceptions (
    exception_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date      TEXT NOT NULL,
    check_name    TEXT NOT NULL,
    severity      TEXT NOT NULL,          -- 'HIGH' | 'MEDIUM' | 'LOW'
    subject       TEXT NOT NULL,          -- what the exception is about
    detail        TEXT NOT NULL           -- human-readable explanation
);
