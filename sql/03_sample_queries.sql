-- ============================================================================
-- SAMPLE QUERIES  --  a tour of the database, easy first.
-- Run these against data/nav.db in DB Browser, under the Execute SQL tab. They
-- build on each other, so read from the top.
-- ============================================================================

-- 1. SELECT / WHERE / ORDER BY  --------------------------------------------
--    The securities the fund can hold, non-EUR ones first.
SELECT ticker, name, currency
FROM security_master
ORDER BY (currency = 'EUR'), name;

-- 2. INNER JOIN  ------------------------------------------------------------
--    Attach each price to its security's name and currency.
SELECT p.price_date, sm.ticker, sm.currency, p.close_price
FROM daily_prices p
JOIN security_master sm ON sm.security_id = p.security_id
WHERE p.price_date = '2024-01-03'
ORDER BY sm.ticker;

-- 3. Multi-table JOIN + arithmetic  ----------------------------------------
--    Value one day's positions in EUR: quantity * price * fx.
SELECT sm.ticker,
       h.quantity,
       p.close_price                                   AS price_local,
       fx.rate_to_eur,
       ROUND(h.quantity * p.close_price * fx.rate_to_eur, 2) AS mkt_value_eur
FROM holdings h
JOIN security_master sm ON sm.security_id = h.security_id
JOIN daily_prices  p   ON p.security_id  = h.security_id AND p.price_date = h.position_date
JOIN fx_rates      fx  ON fx.currency    = sm.currency   AND fx.rate_date = h.position_date
WHERE h.position_date = '2024-01-03'
ORDER BY mkt_value_eur DESC;

-- 4. GROUP BY + SUM (aggregation)  -----------------------------------------
--    Roll the positions up to a single fund gross asset value per day.
SELECT val_date, ROUND(gross_asset_value_eur, 2) AS gav_eur, securities_priced
FROM v_fund_gav
ORDER BY val_date;

-- 5. WINDOW FUNCTION: LAG  --------------------------------------------------
--    Day over day move without a self-join. This is how a NAV jump surfaces.
SELECT val_date,
       ROUND(gross_asset_value_eur, 2)      AS gav_eur,
       ROUND(100.0 * gav_pct_move, 2)       AS pct_move
FROM v_fund_gav_move
ORDER BY val_date;

-- 6. WINDOW FUNCTION: running total  ---------------------------------------
--    Shares outstanding = cumulative net subscriptions per class.
SELECT flow_date, share_class_id, cumulative_net_flow_shares
FROM v_shares_outstanding
ORDER BY share_class_id, flow_date;

-- 7. The output: NAV per share per class per day  --------------------------
SELECT n.nav_date, s.class_name, s.currency,
       ROUND(n.nav_per_share_eur, 4) AS nav_eur,
       ROUND(n.nav_per_share_ccy, 4) AS nav_ccy
FROM nav_daily n
JOIN share_classes s USING (share_class_id)
ORDER BY n.nav_date, n.share_class_id;

-- 8. The exceptions raised by the last reconciliation run  -----------------
SELECT severity, check_name, subject, detail
FROM exceptions
ORDER BY CASE severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END;
