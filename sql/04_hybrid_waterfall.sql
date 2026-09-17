-- ============================================================================
-- HYBRID FUND / PE-STYLE WATERFALL  --  additive schema
-- ----------------------------------------------------------------------------
-- The base schema (01_schema.sql) models a UCITS-style share class: investors
-- subscribe and redeem at a daily struck NAV, and the manager charges a flat
-- management fee. That is the whole of "hedge/mutual fund" fund accounting.
--
-- A hybrid fund adds a second dealing style on top, used by a PE-style class:
-- investors COMMIT capital up front, the manager draws it down over time via
-- CAPITAL CALLS, and returns cash via DISTRIBUTIONS as investments realize.
-- The incentive fee for that class is not a flat performance fee on NAV
-- appreciation -- it is carried interest, paid out through a WATERFALL: return
-- of capital, then a preferred return (hurdle), then a GP catch-up, then a
-- carry split of anything left. See src/waterfall.py for the tier math.
--
-- Which classes are PE-style is a data fact, not a schema fact: a share class
-- with a row in waterfall_config deals via capital_calls/distributions and
-- pays carry; a share class with no row there (the existing EUR Acc / USD
-- Dist classes) keeps dealing via subscriptions_redemptions and a flat
-- management fee, completely untouched by anything in this file.
--
-- A PE-style class's assets are marked separately from the daily-priced
-- liquid book in v_fund_gav (see pe_sleeve_marks below) rather than folded
-- into it. Capital calls buying into the shared GAV would corrupt every
-- class's day-over-day return with a cash-flow-driven jump -- the classic
-- problem performance measurement uses Modified Dietz / time-weighted
-- returns to avoid. Keeping the illiquid sleeve's valuation separate sidesteps
-- that without having to build a full cash-flow-adjusted return calc here.
-- ============================================================================

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS waterfall_ledger;
DROP TABLE IF EXISTS distributions;
DROP TABLE IF EXISTS capital_calls;
DROP TABLE IF EXISTS pe_sleeve_marks;
DROP TABLE IF EXISTS waterfall_config;
DROP TABLE IF EXISTS capital_commitments;

-- ---- PE-style dealing reference data ---------------------------------------

CREATE TABLE capital_commitments (
    share_class_id          INTEGER PRIMARY KEY,
    committed_capital_eur   REAL NOT NULL,   -- total the LPs have committed, called or not
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);

CREATE TABLE waterfall_config (
    share_class_id       INTEGER PRIMARY KEY,  -- presence of a row here IS the "this class is PE-style" flag
    waterfall_type       TEXT NOT NULL,        -- 'EUROPEAN' (whole-fund) | 'AMERICAN' (deal-by-deal)
    hurdle_rate_annual   REAL NOT NULL,        -- e.g. 0.08 = 8% preferred return, compounded actual/365
    catchup_gp_share     REAL NOT NULL,        -- e.g. 1.0 = 100% GP catch-up, 0.5 = 50/50 catch-up
    carry_pct            REAL NOT NULL,        -- e.g. 0.20 = 20% carried interest
    CHECK (waterfall_type IN ('EUROPEAN', 'AMERICAN')),
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);

-- ---- Illiquid sleeve valuation ---------------------------------------------

CREATE TABLE pe_sleeve_marks (
    mark_date        TEXT    NOT NULL,   -- 'YYYY-MM-DD'
    share_class_id   INTEGER NOT NULL,
    sleeve_value_eur REAL    NOT NULL,   -- manager's gross mark, pre-deal, before fees/carry
    PRIMARY KEY (mark_date, share_class_id),
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);
-- Marked less often than the daily-priced book on purpose -- real illiquid
-- assets get a manager mark, not a daily quote. Between marks, calculate_nav.py
-- carries the last mark forward flat (no assumed organic return), adjusted only
-- for that day's own calls and distributions.

-- ---- PE-style dealing -------------------------------------------------------

CREATE TABLE capital_calls (
    call_id         INTEGER PRIMARY KEY,
    call_date       TEXT    NOT NULL,
    share_class_id  INTEGER NOT NULL,
    amount_eur      REAL    NOT NULL,   -- cash drawn down from committed capital, > 0
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);

CREATE TABLE distributions (
    dist_id         INTEGER PRIMARY KEY,
    dist_date       TEXT    NOT NULL,
    share_class_id  INTEGER NOT NULL,
    deal_id         TEXT,               -- which investment this realizes, for AMERICAN mode; NULL = untagged
    amount_eur      REAL    NOT NULL,   -- gross cash distributed, BEFORE the LP/GP waterfall split, > 0
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);

-- ---- Engine output: how each dollar was tiered -----------------------------

CREATE TABLE waterfall_ledger (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date      TEXT    NOT NULL,
    share_class_id  INTEGER NOT NULL,
    tier            TEXT    NOT NULL,   -- RETURN_OF_CAPITAL | PREFERRED_RETURN | GP_CATCHUP_GP | GP_CATCHUP_LP | CARRY_SPLIT_GP | CARRY_SPLIT_LP
    recipient       TEXT    NOT NULL,   -- 'LP' | 'GP'
    amount_eur      REAL    NOT NULL,
    synthetic       INTEGER NOT NULL DEFAULT 0,  -- 1 = mark-to-market accrual, not a real cashflow
    FOREIGN KEY (share_class_id) REFERENCES share_classes(share_class_id)
);
