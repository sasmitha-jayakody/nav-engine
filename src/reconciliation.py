"""
The daily exceptions report, which is the oversight side of the engine.

    python src/reconciliation.py    (run after generate_data and calculate_nav)

Six checks, each one modelled on a control a fund oversight team actually runs:

  1. STALE_PRICE            a price unchanged for N days or more
  2. MISSING_PRICE          a security held on a NAV date with no price
  3. UNRECORDED_CORP_ACTION a big one-day drop with no corporate action booked
  4. PRICE_OUTLIER          a one-day move past a hard threshold
  5. NAV_MOVE_TOLERANCE     fund GAV moving more than tolerance day over day
  6. NAV_CALC_BREAK         stored NAV disagrees with an independent recompute
  7. PE_SLEEVE_BREAK        PE sleeve NAV that does not tie out, or calls over commitment

Findings go into the `exceptions` table and get printed as a report.

The checks are deliberately blunt. Real oversight tolerances are tuned per fund
and get argued over, but the shape of the control is the same.
"""

import os
import sqlite3
from datetime import date

try:
    from src import waterfall
    from src.pe_sleeve import sleeve_cashflows
except ImportError:
    import waterfall
    from pe_sleeve import sleeve_cashflows

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "nav.db")
VALUATION_SQL = os.path.join(ROOT, "sql", "02_nav_calculation.sql")

# ---- tolerances, i.e. the knobs an oversight team sets --------------------
STALE_DAYS = 3           # a price repeated on 3 or more consecutive days
PRICE_OUTLIER_PCT = 0.30 # a one-day security price move over 30%
CORP_ACTION_DROP = 0.35  # a one-day drop over 35% looks like an unbooked split
NAV_MOVE_PCT = 0.03      # a fund GAV move over 3% day over day
NAV_BREAK_EUR = 0.01     # a recompute mismatch over 1 cent counts as a break

LAUNCH_NAV = 100.0
CLASS_SPLIT = {1: 0.70, 2: 0.30}


def query(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def check_stale_prices(con):
    rows = query(con, """SELECT p.price_date, p.security_id, sm.ticker, p.close_price
                         FROM daily_prices p JOIN security_master sm USING (security_id)
                         ORDER BY p.security_id, p.price_date""")
    by_sec = {}
    for r in rows:
        by_sec.setdefault(r["security_id"], []).append(r)
    out = []
    for sid, g in by_sec.items():
        run_start = 0
        for i in range(1, len(g) + 1):
            same = i < len(g) and g[i]["close_price"] == g[i - 1]["close_price"]
            if not same:
                run_len = i - run_start
                if run_len >= STALE_DAYS:
                    out.append(("STALE_PRICE", "MEDIUM", g[run_start]["ticker"],
                                f"price {g[run_start]['close_price']:.2f} unchanged for "
                                f"{run_len} business days "
                                f"({g[run_start]['price_date']}..{g[i-1]['price_date']})"))
                run_start = i
    return out


def check_missing_prices(con):
    rows = query(con, """SELECT h.position_date AS d, sm.ticker
                         FROM holdings h JOIN security_master sm USING (security_id)
                         WHERE NOT EXISTS (
                             SELECT 1 FROM daily_prices p
                             WHERE p.security_id = h.security_id
                               AND p.price_date = h.position_date)
                         ORDER BY h.position_date""")
    return [("MISSING_PRICE", "HIGH", r["ticker"],
             f"held on {r['d']} but no price in daily_prices") for r in rows]


def check_price_moves(con):
    """One pass over price moves, catching outliers and suspected unbooked splits.

    A large fall is treated as a probable unbooked split first, since that is the
    more specific explanation. Anything else large enough is an outlier.
    """
    rows = query(con, """SELECT p.price_date, p.security_id, sm.ticker, p.close_price
                         FROM daily_prices p JOIN security_master sm USING (security_id)
                         ORDER BY p.security_id, p.price_date""")
    booked = set((r["security_id"], r["ex_date"]) for r in
                 query(con, "SELECT security_id, ex_date FROM corporate_actions WHERE ca_type='SPLIT'"))
    by_sec = {}
    for r in rows:
        by_sec.setdefault(r["security_id"], []).append(r)
    out = []
    for sid, g in by_sec.items():
        for i in range(1, len(g)):
            move = g[i]["close_price"] / g[i - 1]["close_price"] - 1.0
            d, tick = g[i]["price_date"], g[i]["ticker"]
            if move <= -CORP_ACTION_DROP and (sid, d) not in booked:
                out.append(("UNRECORDED_CORP_ACTION", "HIGH", tick,
                            f"price fell {move:.1%} on {d} with no corporate action "
                            f"booked (looks like an unrecorded split)"))
            elif abs(move) > PRICE_OUTLIER_PCT and (sid, d) not in booked:
                out.append(("PRICE_OUTLIER", "HIGH", tick,
                            f"one-day price move of {move:+.1%} on {d} exceeds "
                            f"{PRICE_OUTLIER_PCT:.0%} threshold (possible fat-finger)"))
    return out


def check_nav_moves(con):
    out = []
    for r in query(con, "SELECT val_date, gav_pct_move FROM v_fund_gav_move ORDER BY val_date"):
        m = r["gav_pct_move"]
        if m is not None and abs(m) > NAV_MOVE_PCT:
            out.append(("NAV_MOVE_TOLERANCE", "MEDIUM", "Fund GAV",
                        f"gross asset value moved {m:+.1%} on {r['val_date']} "
                        f"(tolerance +/-{NAV_MOVE_PCT:.0%})"))
    return out


def shadow_nav(con):
    """A second, independent implementation of the per class roll forward.

    Written separately from calculate_nav.py on purpose. If the two agreed by
    sharing code they would agree on the mistakes too, so a booking error in the
    production output (planted error 5) would sail through unnoticed.
    """
    gav = query(con, "SELECT val_date, gross_asset_value_eur FROM v_fund_gav ORDER BY val_date")
    classes = query(con, "SELECT share_class_id, currency, mgmt_fee_bps FROM share_classes "
                         "ORDER BY share_class_id")
    flows = query(con, "SELECT flow_date, share_class_id, flow_type, shares "
                       "FROM subscriptions_redemptions")
    fx = {}
    for r in query(con, "SELECT rate_date, currency, rate_to_eur FROM fx_rates"):
        fx[(r["rate_date"], r["currency"])] = r["rate_to_eur"]
    div_by = {}
    for r in query(con, """SELECT ca.pay_date d,
                                  ca.amount_per_share*h.quantity*fx.rate_to_eur inc
                           FROM corporate_actions ca
                           JOIN holdings h ON h.security_id=ca.security_id
                                          AND h.position_date=ca.ex_date
                           JOIN security_master sm ON sm.security_id=ca.security_id
                           JOIN fx_rates fx ON fx.currency=sm.currency
                                           AND fx.rate_date=ca.pay_date
                           WHERE ca.ca_type='DIVIDEND'"""):
        div_by[r["d"]] = div_by.get(r["d"], 0.0) + r["inc"]

    dates = [r["val_date"] for r in gav]
    g = {r["val_date"]: r["gross_asset_value_eur"] for r in gav}

    st = {}
    for c in classes:
        cid = c["share_class_id"]
        na0 = g[dates[0]] * CLASS_SPLIT[cid]
        st[cid] = {"na": na0, "sh": na0 / LAUNCH_NAV,
                   "ccy": c["currency"], "bps": c["mgmt_fee_bps"]}

    recomputed = {}
    for d in dates[1:]:
        prev_d = dates[dates.index(d) - 1]
        ret = g[d] / g[prev_d]
        inc = div_by.get(d, 0.0)
        tot = sum(s["na"] for s in st.values())
        for cid, s in st.items():
            s["na"] = s["na"] * ret + inc * (s["na"] / tot)
            s["na"] -= s["na"] * (s["bps"] / 10000.0) / 365.0
            nav_eur = s["na"] / s["sh"]
            net = 0.0
            for f in flows:
                if f["flow_date"] == d and f["share_class_id"] == cid:
                    net += f["shares"] if f["flow_type"] == "SUB" else -f["shares"]
            s["na"] += net * nav_eur
            s["sh"] += net
            recomputed[(d, cid)] = nav_eur
    return recomputed


def check_nav_break(con):
    stored = query(con, "SELECT nav_date, share_class_id, nav_per_share_eur FROM nav_daily")
    names = {r["share_class_id"]: r["class_name"] for r in
             query(con, "SELECT share_class_id, class_name FROM share_classes")}
    recomputed = shadow_nav(con)
    out = []
    for r in stored:
        exp = recomputed.get((r["nav_date"], r["share_class_id"]))
        if exp is not None and abs(exp - r["nav_per_share_eur"]) > NAV_BREAK_EUR:
            out.append(("NAV_CALC_BREAK", "HIGH", names[r["share_class_id"]],
                        f"stored NAV {r['nav_per_share_eur']:.4f} EUR on {r['nav_date']} vs "
                        f"independent recompute {exp:.4f} EUR "
                        f"(break {r['nav_per_share_eur']-exp:+.4f})"))
    return out


def check_pe_sleeve(con):
    """Ties out the PE sleeve NAV without replaying its roll forward.

    For every quarter end:
      * units x NAV per unit has to equal net assets
      * net assets has to equal mark, less fee, less carry accrued but unpaid,
        plus the day's calls, less the day's gross distributions
      * carry accrued has to match a fresh waterfall run on the raw calls,
        distributions and mark
    And across the life of the sleeve, calls cannot exceed the commitment.
    """
    out = []
    for sl in query(con, "SELECT * FROM pe_sleeve"):
        sid, name = sl["sleeve_id"], sl["sleeve_name"]
        calls = query(con, "SELECT call_date, deal_id, amount_eur FROM pe_capital_calls "
                           "WHERE sleeve_id = ?", (sid,))
        dists = query(con, "SELECT dist_date, deal_id, amount_eur FROM pe_distributions "
                           "WHERE sleeve_id = ?", (sid,))

        called = sum(c["amount_eur"] for c in calls)
        if called > sl["committed_eur"] + NAV_BREAK_EUR:
            out.append(("PE_SLEEVE_BREAK", "HIGH", name,
                        f"calls of {called:,.0f} EUR exceed the {sl['committed_eur']:,.0f} EUR commitment"))

        for r in query(con, "SELECT * FROM pe_sleeve_nav WHERE sleeve_id = ? ORDER BY nav_date", (sid,)):
            d = r["nav_date"]
            call_today = sum(c["amount_eur"] for c in calls if c["call_date"] == d)
            cash_out = sum(x["amount_eur"] for x in dists if x["dist_date"] == d)

            if abs(r["units_outstanding"] * r["nav_per_unit_eur"] - r["net_assets_eur"]) > 1.0:
                out.append(("PE_SLEEVE_BREAK", "HIGH", name,
                            f"units x NAV does not equal net assets on {d}"))

            expected = (r["gross_value_eur"] - r["mgmt_fee_eur"]
                        - (r["carry_accrued_eur"] - r["carry_paid_eur"])
                        + call_today - cash_out)
            if abs(expected - r["net_assets_eur"]) > 1.0:
                out.append(("PE_SLEEVE_BREAK", "HIGH", name,
                            f"net assets {r['net_assets_eur']:,.2f} on {d} vs {expected:,.2f} "
                            f"from mark, fee, carry and cash"))

            carry = waterfall.run_waterfall(
                sleeve_cashflows(calls, dists, d), sl["hurdle_rate"], sl["catchup_gp_share"],
                sl["carry_pct"], mode=sl["waterfall_type"], as_of_date=d,
                unrealized_value=max(r["gross_value_eur"] - r["mgmt_fee_eur"] - cash_out, 0.0),
            ).gp_total()
            if abs(carry - r["carry_accrued_eur"]) > 1.0:
                out.append(("PE_SLEEVE_BREAK", "HIGH", name,
                            f"carry accrued {r['carry_accrued_eur']:,.2f} on {d} vs "
                            f"{carry:,.2f} from a fresh waterfall run"))
    return out


def run():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON;")
    with open(VALUATION_SQL) as f:
        con.executescript(f.read())

    findings = []
    for fn in (check_stale_prices, check_missing_prices, check_price_moves,
               check_nav_moves, check_nav_break, check_pe_sleeve):
        findings += fn(con)

    run_date = date.today().isoformat()
    con.execute("DELETE FROM exceptions")
    con.executemany(
        "INSERT INTO exceptions (run_date, check_name, severity, subject, detail) "
        "VALUES (?,?,?,?,?)",
        [(run_date, c, sev, subj, det) for (c, sev, subj, det) in findings])
    con.commit()
    con.close()

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(key=lambda x: order[x[1]])
    print("=" * 78)
    print(f"  DAILY EXCEPTIONS REPORT   run {run_date}   {len(findings)} exception(s)")
    print("=" * 78)
    for check, sev, subj, det in findings:
        print(f"[{sev:<6}] {check:<22} {subj}")
        print(f"          {det}")
    print("=" * 78)
    return findings


if __name__ == "__main__":
    run()
