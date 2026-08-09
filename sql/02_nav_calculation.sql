-- ============================================================================
-- NAV CALCULATION  --  the set-based valuation, in SQL
-- ----------------------------------------------------------------------------
-- Valuation is the part SQL is good at. For every date, take every holding,
-- multiply by that day's price, convert to EUR, sum. A join across four tables
-- and a GROUP BY.
--
-- The rest of multi-class accounting, meaning rolling shares outstanding, per
-- class fees and dealing at the struck NAV, is sequential: each day needs the
-- day before it. That part sits in Python, in src/calculate_nav.py. A recursive
-- CTE could bring it back into SQL.
--
-- These views are what the Python engine reads. They also happen to run through
-- most of the SQL the project depends on, in order.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- VIEW 1: position-level valuation.
-- One row per (date, security): quantity * price * fx = market value in EUR.
-- INNER JOIN across holdings, prices, security master and fx, then arithmetic.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_position_valuation;
CREATE VIEW v_position_valuation AS
SELECT
    h.position_date              AS val_date,
    h.security_id                AS security_id,
    sm.ticker                    AS ticker,
    sm.currency                  AS ccy,
    h.quantity                   AS quantity,
    p.close_price                AS price_local,
    fx.rate_to_eur               AS fx_to_eur,
    h.quantity * p.close_price * fx.rate_to_eur AS market_value_eur
FROM holdings h
JOIN security_master sm ON sm.security_id = h.security_id
JOIN daily_prices  p   ON p.security_id  = h.security_id
                      AND p.price_date   = h.position_date
JOIN fx_rates      fx  ON fx.currency    = sm.currency
                      AND fx.rate_date   = h.position_date;
-- Note the inner join on prices. A security with no price on a date (planted
-- error 4) drops straight out of the valuation for that day, and the fund is
-- quietly undervalued with nothing here to show for it. Catching that gap is
-- left to the reconciliation layer, which is the point of having one.

-- ---------------------------------------------------------------------------
-- VIEW 2: fund gross asset value per day.
-- GROUP BY with SUM, plus a row count so a missing price is visible as a gap.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_fund_gav;
CREATE VIEW v_fund_gav AS
SELECT
    val_date,
    SUM(market_value_eur)  AS gross_asset_value_eur,
    COUNT(*)               AS securities_priced
FROM v_position_valuation
GROUP BY val_date;

-- ---------------------------------------------------------------------------
-- VIEW 3: day-over-day GAV move.
-- LAG() reaches back into the previous row once the rows are ordered by date,
-- so today and yesterday can be compared without a self-join.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_fund_gav_move;
CREATE VIEW v_fund_gav_move AS
SELECT
    val_date,
    gross_asset_value_eur,
    LAG(gross_asset_value_eur) OVER (ORDER BY val_date) AS prev_gav_eur,
    gross_asset_value_eur
        / LAG(gross_asset_value_eur) OVER (ORDER BY val_date) - 1.0 AS gav_pct_move
FROM v_fund_gav;

-- ---------------------------------------------------------------------------
-- VIEW 4: shares outstanding per class per day.
-- A running total with SUM() OVER (... ORDER BY ...). Net flow shares are +SUB
-- and -RED, so the cumulative sum up to a date is shares outstanding. The Python
-- engine tracks this too, and this view is how you check it against SQL.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_shares_outstanding;
CREATE VIEW v_shares_outstanding AS
WITH signed_flows AS (
    SELECT
        flow_date,
        share_class_id,
        CASE WHEN flow_type = 'SUB' THEN shares ELSE -shares END AS net_shares
    FROM subscriptions_redemptions
)
SELECT
    flow_date,
    share_class_id,
    SUM(SUM(net_shares)) OVER (
        PARTITION BY share_class_id ORDER BY flow_date
    ) AS cumulative_net_flow_shares
FROM signed_flows
GROUP BY flow_date, share_class_id;
