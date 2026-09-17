"""
Strikes the daily NAV per share class. This is the production side of the
engine, and reconciliation.py is what checks its output.

    python src/calculate_nav.py     (needs data/nav.db from generate_data.py)

Valuing the portfolio happens in SQL, in the v_fund_gav view defined by
sql/02_nav_calculation.sql. What is left is the accounting roll forward, which
lives here because each day depends on the day before:

  for each NAV date and each OPEN-ENDED share class (EUR Acc, USD Dist)
    1. net assets grow with the return on the shared portfolio
    2. add the class's share of any dividend income received
    3. accrue the class's own management fee, which reduces NAV
    4. strike NAV per share on pre-deal net assets / pre-deal shares
    5. deal subscriptions and redemptions at that struck NAV (forward pricing)
    6. convert NAV per share into the class currency

The invariant holding it together: the class net assets always add back up to
the fund's independently valued net assets. reconciliation.py rechecks that with
a shadow calculation written separately from this one.

Left out on purpose: no trading after inception, one pricing point a day, no
income equalisation, no tax. Fees accrue daily on actual/365.

Hybrid fund: run_pe_sleeve(), below, does the equivalent roll forward for the
PE-style class(es) -- any share class with a row in waterfall_config. It deals
via capital calls / distributions instead of subs/reds, marks its assets from
pe_sleeve_marks instead of the shared v_fund_gav, and its incentive fee is a
waterfall-derived carry accrual instead of nothing (open-ended classes here
only ever pay a flat management fee). See sql/04_hybrid_waterfall.sql and
src/waterfall.py for the reasoning.
"""

import os
import sqlite3

try:
    from src import waterfall          # `python run.py` / `from src import calculate_nav`
except ImportError:
    import waterfall                   # `python src/calculate_nav.py` run directly

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "nav.db")
VALUATION_SQL = os.path.join(ROOT, "sql", "02_nav_calculation.sql")

LAUNCH_NAV = 100.0                  # NAV per share at inception
CLASS_SPLIT = {1: 0.70, 2: 0.30}    # each open-ended class's share of net assets at inception


def query(con, sql, params=()):
    """Run a SELECT and hand back dict rows. Saves pulling in pandas for this."""
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load(con):
    con.executescript(open(VALUATION_SQL).read())  # (re)create the views

    gav = query(con, "SELECT val_date, gross_asset_value_eur FROM v_fund_gav ORDER BY val_date")
    classes = query(con, "SELECT share_class_id, class_name, currency, mgmt_fee_bps "
                         "FROM share_classes ORDER BY share_class_id")
    flows = query(con, "SELECT flow_date, share_class_id, flow_type, shares "
                       "FROM subscriptions_redemptions")

    fx = {}
    for r in query(con, "SELECT rate_date, currency, rate_to_eur FROM fx_rates"):
        fx[(r["rate_date"], r["currency"])] = r["rate_to_eur"]

    # Dividend income received by the fund, in EUR, per pay_date.
    div_by_date = {}
    for r in query(con, """
        SELECT ca.pay_date AS pay_date,
               ca.amount_per_share * h.quantity * fx.rate_to_eur AS income_eur
        FROM corporate_actions ca
        JOIN holdings  h  ON h.security_id = ca.security_id
                         AND h.position_date = ca.ex_date
        JOIN security_master sm ON sm.security_id = ca.security_id
        JOIN fx_rates fx ON fx.currency = sm.currency
                        AND fx.rate_date = ca.pay_date
        WHERE ca.ca_type = 'DIVIDEND'
    """):
        div_by_date[r["pay_date"]] = div_by_date.get(r["pay_date"], 0.0) + r["income_eur"]

    return gav, classes, flows, fx, div_by_date


def fx_to_eur(fx, ccy, d):
    return 1.0 if ccy == "EUR" else fx[(d, ccy)]


def net_flow_shares(flows, d, cid):
    total = 0.0
    for f in flows:
        if f["flow_date"] == d and f["share_class_id"] == cid:
            total += f["shares"] if f["flow_type"] == "SUB" else -f["shares"]
    return total


def run_pe_sleeve(con, nav_dates):
    """The PE-style roll forward: one pass per NAV date for every share class
    that has a waterfall_config row. Mirrors run()'s open-ended loop step for
    step, but marks its own asset base and prices its incentive fee off the
    waterfall instead of a flat bps rate.

      for each NAV date and each PE-style share class
        1. take the manager's mark (pre-deal), or carry the last one forward
        2. accrue the class's own management fee, same actual/365 convention
        3. re-run the whole cashflow history through the waterfall, as of
           today, with today's post-fee value as the unrealized top-up --
           the DELTA in the GP's cumulative entitlement is today's carry accrual
        4. strike NAV per share on pre-deal net assets / pre-deal shares
        5. deal today's capital call (like a subscription) or distribute the
           LP's share of today's distribution (like a redemption) -- the GP's
           share was already recognised in step 3, so only the LP leg moves
           net assets again here
        6. convert NAV per share into the class currency

    Returns (nav_rows, fee_rows, ledger_rows) shaped for the same nav_daily /
    fee_accruals / waterfall_ledger inserts run() already does for the
    open-ended classes.
    """
    configs = query(con, "SELECT share_class_id, waterfall_type, hurdle_rate_annual, "
                         "catchup_gp_share, carry_pct FROM waterfall_config")
    if not configs:
        return [], [], []

    bps_by_class = {c["share_class_id"]: c["mgmt_fee_bps"] for c in
                    query(con, "SELECT share_class_id, mgmt_fee_bps FROM share_classes")}
    ccy_by_class = {c["share_class_id"]: c["currency"] for c in
                    query(con, "SELECT share_class_id, currency FROM share_classes")}
    calls = query(con, "SELECT call_date, share_class_id, amount_eur FROM capital_calls ORDER BY call_date")
    dists = query(con, "SELECT dist_date, share_class_id, deal_id, amount_eur "
                       "FROM distributions ORDER BY dist_date")
    marks = query(con, "SELECT mark_date, share_class_id, sleeve_value_eur FROM pe_sleeve_marks")
    fx = {}
    for r in query(con, "SELECT rate_date, currency, rate_to_eur FROM fx_rates"):
        fx[(r["rate_date"], r["currency"])] = r["rate_to_eur"]

    nav_rows, fee_rows, ledger_rows = [], [], []

    for cfg in configs:
        cid = cfg["share_class_id"]
        mode = cfg["waterfall_type"]
        hurdle, catchup, carry = cfg["hurdle_rate_annual"], cfg["catchup_gp_share"], cfg["carry_pct"]
        bps, ccy = bps_by_class[cid], ccy_by_class[cid]

        cls_calls = [c for c in calls if c["share_class_id"] == cid]
        cls_dists = [d for d in dists if d["share_class_id"] == cid]
        marks_by_date = {m["mark_date"]: m["sleeve_value_eur"] for m in marks if m["share_class_id"] == cid}
        calls_by_date = {}
        for c in cls_calls:
            calls_by_date[c["call_date"]] = calls_by_date.get(c["call_date"], 0.0) + c["amount_eur"]
        dists_by_date = {}
        for d in cls_dists:
            dists_by_date.setdefault(d["dist_date"], []).append(d)

        gross = accrued_mgmt_fee_cum = accrued_carry_cum = na = sh = 0.0

        for d in nav_dates:
            if d in marks_by_date:
                gross = marks_by_date[d]  # new manager mark, pre-deal
            # else: carry the last gross value forward flat -- no organic
            # move is assumed for an illiquid asset between marks.

            mgmt_fee_today = gross * (bps / 10000.0) / 365.0
            accrued_mgmt_fee_cum += mgmt_fee_today
            distributable_value_today = gross - accrued_mgmt_fee_cum

            call_amt = calls_by_date.get(d, 0.0)
            today_dists = dists_by_date.get(d, [])
            dist_amt_today = sum(x["amount_eur"] for x in today_dists)

            # A call's cash only merges into the pool once it's dealt (gross
            # is bumped by call_amt at the END of the loop below, becoming
            # tomorrow's baseline) -- so strictly before today, not through
            # today, or the waterfall would see a bigger return-of-capital
            # bar with no matching asset to fund it and manufacture a fake
            # loss on call day. A distribution IS included through today: we
            # need it tiered today to know how to split today's payout.
            cashflows = [(c["call_date"], "CALL", c["amount_eur"])
                         for c in cls_calls if c["call_date"] < d]
            cashflows += [(x["dist_date"], "DIST", x["amount_eur"], x["deal_id"] or waterfall.DEFAULT_DEAL)
                          for x in cls_dists if x["dist_date"] <= d]

            remaining_unrealized = max(distributable_value_today - dist_amt_today, 0.0)
            result = waterfall.run_waterfall(cashflows, hurdle, catchup, carry, mode=mode,
                                              as_of_date=d, unrealized_value=remaining_unrealized)

            gp_cum_total_today = result.gp_total()
            incentive_fee_today = gp_cum_total_today - accrued_carry_cum
            accrued_carry_cum = gp_cum_total_today

            # 4. strike NAV per share on pre-deal net assets / pre-deal shares
            na_before_deal = distributable_value_today - accrued_carry_cum
            nav_ps_eur = na_before_deal / sh if sh > 0 else LAUNCH_NAV

            # 5. deal today's call/distribution at that struck NAV. The GP's
            # cut of today's distribution is already reflected in
            # accrued_carry_cum above, so only the LP leg moves na/shares here.
            lp_amt = 0.0
            if today_dists:
                lp_amt, _gp_amt = result.event_split(d)
            na_after = na_before_deal + call_amt - lp_amt
            sh_after = sh + (call_amt / nav_ps_eur if call_amt else 0.0) \
                          - (lp_amt / nav_ps_eur if lp_amt else 0.0)
            gross = gross + call_amt - lp_amt

            # 6. NAV per share in the class currency
            nav_ps_ccy = nav_ps_eur / fx_to_eur(fx, ccy, d)
            nav_rows.append((d, cid, na_after, sh_after, nav_ps_eur, nav_ps_ccy))
            fee_rows.append((d, cid, mgmt_fee_today, "MGMT"))
            fee_rows.append((d, cid, incentive_fee_today, "INCENTIVE"))
            for row in result.ledger:
                if row.event_date == d:
                    ledger_rows.append((d, cid, row.tier, row.recipient, row.amount,
                                        1 if row.synthetic else 0))

            na, sh = na_after, sh_after

    return nav_rows, fee_rows, ledger_rows


def run():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON;")
    gav, classes, flows, fx, div_by_date = load(con)

    dates = [r["val_date"] for r in gav]
    gav_by_date = {r["val_date"]: r["gross_asset_value_eur"] for r in gav}
    inception, nav_dates = dates[0], dates[1:]

    # ---- initial state at inception --------------------------------------
    # Only the open-ended classes take a slice of the shared GAV at launch.
    # PE-style classes (waterfall_config) start empty and are funded later by
    # their own capital calls -- see run_pe_sleeve().
    gav0 = gav_by_date[inception]
    state = {}  # share_class_id -> dict of running figures
    for c in classes:
        if c["share_class_id"] not in CLASS_SPLIT:
            continue
        cid = c["share_class_id"]
        na0 = gav0 * CLASS_SPLIT[cid]
        state[cid] = {"na": na0, "sh": na0 / LAUNCH_NAV,
                      "ccy": c["currency"], "fee_bps": c["mgmt_fee_bps"],
                      "name": c["class_name"]}

    fee_rows, nav_rows = [], []

    # ---- daily roll-forward ----------------------------------------------
    for d in nav_dates:
        prev_d = dates[dates.index(d) - 1]
        ret = gav_by_date[d] / gav_by_date[prev_d]        # shared portfolio return
        div_income = div_by_date.get(d, 0.0)
        total_open = sum(s["na"] for s in state.values())

        for cid, s in state.items():
            # 1-2. grow with the shared return, plus pro rata dividend income
            s["na"] = s["na"] * ret + div_income * (s["na"] / total_open)
            # 3. accrue this class's own management fee (actual/365)
            fee = s["na"] * (s["fee_bps"] / 10000.0) / 365.0
            s["na"] -= fee
            fee_rows.append((d, cid, fee))
            # 4. strike NAV per share on pre-deal net assets and pre-deal shares
            nav_ps_eur = s["na"] / s["sh"]
            # 5. deal subs/reds in shares at the struck NAV (forward pricing)
            nshares = net_flow_shares(flows, d, cid)
            s["na"] += nshares * nav_ps_eur
            s["sh"] += nshares
            # 6. NAV per share in the class currency
            nav_ps_ccy = nav_ps_eur / fx_to_eur(fx, s["ccy"], d)
            nav_rows.append((d, cid, s["na"], s["sh"], nav_ps_eur, nav_ps_ccy))

    # ---- PE-style class(es): capital calls, distributions, waterfall carry -
    pe_nav_rows, pe_fee_rows, pe_ledger_rows = run_pe_sleeve(con, nav_dates)
    nav_rows += pe_nav_rows

    # ---- persist ----------------------------------------------------------
    con.execute("DELETE FROM fee_accruals")
    con.execute("DELETE FROM nav_daily")
    con.execute("DELETE FROM waterfall_ledger")
    con.executemany(
        "INSERT INTO fee_accruals (accrual_date, share_class_id, fee_amount) VALUES (?,?,?)",
        fee_rows,
    )
    con.executemany(
        "INSERT INTO fee_accruals (accrual_date, share_class_id, fee_amount, fee_type) VALUES (?,?,?,?)",
        pe_fee_rows,
    )
    con.executemany("INSERT INTO nav_daily VALUES (?,?,?,?,?,?)", nav_rows)
    con.executemany(
        "INSERT INTO waterfall_ledger (event_date, share_class_id, tier, recipient, amount_eur, synthetic) "
        "VALUES (?,?,?,?,?,?)",
        pe_ledger_rows,
    )

    # ---- PLANTED ERROR 5: a bad manual NAV override -----------------------
    # A mistyped NAV correction booked straight into the output: EUR Acc (class 1)
    # bumped 2% on 2024-01-29. The shadow calculation in reconciliation.py has no
    # way to reproduce it, so it comes out as a calc break.
    con.execute("""
        UPDATE nav_daily
        SET nav_per_share_eur = nav_per_share_eur * 1.02,
            nav_per_share_ccy = nav_per_share_ccy * 1.02
        WHERE nav_date = '2024-01-29' AND share_class_id = 1
    """)  # PLANTED ERROR (NAV calc break)
    con.commit()

    # ---- print the first and last few rows --------------------------------
    out = query(con, """
        SELECT n.nav_date, s.class_name, s.currency,
               ROUND(n.shares_outstanding, 2) AS shares,
               ROUND(n.nav_per_share_eur, 4)  AS nav_eur,
               ROUND(n.nav_per_share_ccy, 4)  AS nav_ccy
        FROM nav_daily n JOIN share_classes s USING (share_class_id)
        ORDER BY n.nav_date, n.share_class_id
    """)
    n_classes = len({row[1] for row in nav_rows})  # distinct share_class_id across both loops
    con.close()

    print(f"Struck NAV for {len(nav_dates)} dates x {n_classes} classes "
          f"({len(state)} open-ended, {len(pe_ledger_rows) and 1 or 0} PE-style).\n")
    hdr = f"{'nav_date':<11}{'class':<10}{'ccy':<5}{'shares':>12}{'nav_eur':>11}{'nav_ccy':>11}"
    print(hdr)
    print("-" * len(hdr))
    for r in out[:4] + out[-4:]:
        print(f"{r['nav_date']:<11}{r['class_name']:<10}{r['currency']:<5}"
              f"{r['shares']:>12}{r['nav_eur']:>11}{r['nav_ccy']:>11}")

    if pe_ledger_rows:
        # Each day's synthetic (mark-to-market) rows are a full as-of-today
        # snapshot, not a delta, so only the LAST day's synthetic rows belong
        # in a cumulative total -- real (non-synthetic) rows each happen once
        # and always sum safely.
        last_date = max(r[0] for r in pe_ledger_rows)
        real = [r for r in pe_ledger_rows if not r[5]]
        final_snapshot = [r for r in pe_ledger_rows if r[5] and r[0] == last_date]
        counted = real + final_snapshot
        gp_total = sum(r[4] for r in counted if r[3] == "GP")
        lp_total = sum(r[4] for r in counted if r[3] == "LP")
        print(f"\nPE Sleeve waterfall as of {last_date}: LP {lp_total:,.0f} EUR, "
              f"GP carry {gp_total:,.0f} EUR cumulative ({len(real)} tier row(s) realized, "
              f"rest mark-to-market).")
    return out


if __name__ == "__main__":
    run()
