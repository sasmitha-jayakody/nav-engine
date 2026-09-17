-- ============================================================================
-- PE SLEEVE: schema for the fund's private equity sleeve
-- ----------------------------------------------------------------------------
-- The sleeve is a separate book under the same umbrella as the liquid fund in
-- 01_schema.sql. It has its own assets, its own investors and its own NAV, so
-- it is not a share class. Share classes all own the same portfolio.
--
-- It is closed-end. Investors commit capital, the manager calls it as deals
-- come up, and cash comes back as distributions. The sleeve keeps units so it
-- can report NAV per unit: a call issues units, and a distribution pays cash
-- without touching the units, so NAV per unit falls. Plenty of closed-end
-- funds keep partner capital accounts instead.
--
-- Valuation is quarterly, from a manager mark. Carry is accrued each quarter
-- on a hypothetical liquidation (HLBV) basis: run the waterfall as if the
-- sleeve sold everything at the mark, and book the GP's share as a liability.
-- ============================================================================

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS pe_waterfall_ledger;
DROP TABLE IF EXISTS pe_sleeve_nav;
DROP TABLE IF EXISTS pe_valuations;
DROP TABLE IF EXISTS pe_distributions;
DROP TABLE IF EXISTS pe_capital_calls;
DROP TABLE IF EXISTS pe_sleeve;

CREATE TABLE pe_sleeve (
    sleeve_id         INTEGER PRIMARY KEY,
    sleeve_name       TEXT    NOT NULL,
    currency          TEXT    NOT NULL,
    committed_eur     REAL    NOT NULL,   -- total investor commitment
    mgmt_fee_bps      INTEGER NOT NULL,   -- annual, charged on the quarterly mark
    waterfall_type    TEXT    NOT NULL,   -- 'EUROPEAN' or 'AMERICAN'
    hurdle_rate       REAL    NOT NULL,   -- preferred return, e.g. 0.08
    catchup_gp_share  REAL    NOT NULL,   -- 1.0 is a full GP catch-up
    carry_pct         REAL    NOT NULL,   -- e.g. 0.20
    launch_nav        REAL    NOT NULL,   -- unit price for the first call
    CHECK (waterfall_type IN ('EUROPEAN', 'AMERICAN'))
);

CREATE TABLE pe_capital_calls (
    call_id     INTEGER PRIMARY KEY,
    sleeve_id   INTEGER NOT NULL,
    call_date   TEXT    NOT NULL,        -- falls on a valuation date
    deal_id     TEXT    NOT NULL,        -- the investment the cash is for
    amount_eur  REAL    NOT NULL,
    FOREIGN KEY (sleeve_id) REFERENCES pe_sleeve(sleeve_id)
);

CREATE TABLE pe_distributions (
    dist_id     INTEGER PRIMARY KEY,
    sleeve_id   INTEGER NOT NULL,
    dist_date   TEXT    NOT NULL,        -- falls on a valuation date
    deal_id     TEXT    NOT NULL,        -- the investment that was sold
    amount_eur  REAL    NOT NULL,        -- gross, before the LP / GP split
    FOREIGN KEY (sleeve_id) REFERENCES pe_sleeve(sleeve_id)
);

CREATE TABLE pe_valuations (
    valuation_date   TEXT    NOT NULL,   -- quarter end
    sleeve_id        INTEGER NOT NULL,
    gross_value_eur  REAL    NOT NULL,   -- manager mark before that day's calls and distributions
    PRIMARY KEY (valuation_date, sleeve_id),
    FOREIGN KEY (sleeve_id) REFERENCES pe_sleeve(sleeve_id)
);

-- ---- Engine output ---------------------------------------------------------

CREATE TABLE pe_sleeve_nav (
    nav_date            TEXT    NOT NULL,
    sleeve_id           INTEGER NOT NULL,
    gross_value_eur     REAL    NOT NULL,  -- the mark
    mgmt_fee_eur        REAL    NOT NULL,  -- fee for the quarter, paid on the day
    carry_accrued_eur   REAL    NOT NULL,  -- GP carry to date on an HLBV basis, paid or not
    carry_paid_eur      REAL    NOT NULL,  -- GP carry actually paid out to date
    net_assets_eur      REAL    NOT NULL,  -- after the day's calls and distributions
    units_outstanding   REAL    NOT NULL,  -- only changes when capital is called
    nav_per_unit_eur    REAL    NOT NULL,  -- after the day's calls and distributions
    PRIMARY KEY (nav_date, sleeve_id),
    FOREIGN KEY (sleeve_id) REFERENCES pe_sleeve(sleeve_id)
);

CREATE TABLE pe_waterfall_ledger (
    row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve_id   INTEGER NOT NULL,
    dist_date   TEXT    NOT NULL,
    deal_id     TEXT    NOT NULL,
    tier        TEXT    NOT NULL,          -- RETURN_OF_CAPITAL, PREFERRED_RETURN, CATCHUP, CARRY
    recipient   TEXT    NOT NULL,          -- 'LP' or 'GP'
    amount_eur  REAL    NOT NULL,
    FOREIGN KEY (sleeve_id) REFERENCES pe_sleeve(sleeve_id)
);
-- Only real distributions are written here, so the rows always add up to
-- the cash that actually went out.
