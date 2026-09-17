"""
Quarterly NAV for the PE sleeve.

    python src/pe_sleeve.py     (needs data/nav.db from generate_data.py)

The sleeve is valued once a quarter. On each valuation date:

  1. take the manager's mark, before the day's calls and distributions
  2. charge the management fee for the quarter on that mark
  3. run the waterfall over every call and distribution so far, with what is
     left of the mark treated as sold today. The GP's total is carry accrued.
  4. strike NAV per unit: mark, less fee, less carry accrued but not paid,
     divided by units in issue
  5. deal at that NAV. A call issues units. A distribution redeems the LP
     share of the cash, and the GP share is recorded as carry paid.
"""

import os
import sqlite3
from datetime import date

try:
    from src import waterfall
except ImportError:
    import waterfall

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "nav.db")


def query(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def sleeve_cashflows(calls, dists, on_date):
    """Calls before on_date and distributions up to and including it.

    A call made on on_date is left out: its cash has not reached the mark yet,
    so counting it would raise the capital to return with nothing to fund it.
    A distribution on on_date is kept, because today's split is needed to pay it.
    """
    flows = [(c["call_date"], "CALL", c["amount_eur"], c["deal_id"])
             for c in calls if c["call_date"] < on_date]
    flows += [(d["dist_date"], "DIST", d["amount_eur"], d["deal_id"])
              for d in dists if d["dist_date"] <= on_date]
    return flows


def strike(con, sleeve):
    sid = sleeve["sleeve_id"]
    marks = query(con, "SELECT valuation_date, gross_value_eur FROM pe_valuations "
                       "WHERE sleeve_id = ? ORDER BY valuation_date", (sid,))
    calls = query(con, "SELECT call_date, deal_id, amount_eur FROM pe_capital_calls "
                       "WHERE sleeve_id = ? ORDER BY call_date", (sid,))
    dists = query(con, "SELECT dist_date, deal_id, amount_eur FROM pe_distributions "
                       "WHERE sleeve_id = ? ORDER BY dist_date", (sid,))

    nav_rows, ledger_rows = [], []
    units = carry_paid = 0.0
    prev_date = None

    for m in marks:
        d, gross = m["valuation_date"], m["gross_value_eur"]
        call_today = sum(c["amount_eur"] for c in calls if c["call_date"] == d)
        dist_today = [x for x in dists if x["dist_date"] == d]
        cash_out = sum(x["amount_eur"] for x in dist_today)

        days = days_between(prev_date, d) if prev_date else 0
        fee = gross * sleeve["mgmt_fee_bps"] / 10000.0 * days / 365.0

        result = waterfall.run_waterfall(
            sleeve_cashflows(calls, dists, d),
            sleeve["hurdle_rate"], sleeve["catchup_gp_share"], sleeve["carry_pct"],
            mode=sleeve["waterfall_type"], as_of_date=d,
            unrealized_value=max(gross - fee - cash_out, 0.0),
        )
        carry_accrued = result.gp_total()

        net_before = gross - fee - (carry_accrued - carry_paid)
        nav = net_before / units if units > 0 else sleeve["launch_nav"]

        lp_cash, gp_cash = result.split_on(d)
        units += call_today / nav - lp_cash / nav
        carry_paid += gp_cash
        net_after = net_before + call_today - lp_cash

        nav_rows.append((d, sid, gross, fee, carry_accrued, carry_paid, net_after, units, nav))
        for r in result.ledger:
            if r.event_date == d and not r.as_of:
                ledger_rows.append((sid, d, r.deal_id, r.tier, r.recipient, r.amount))
        prev_date = d

    return nav_rows, ledger_rows


def run():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON;")
    nav_rows, ledger_rows = [], []
    for sleeve in query(con, "SELECT * FROM pe_sleeve"):
        n, l = strike(con, sleeve)
        nav_rows += n
        ledger_rows += l

    con.execute("DELETE FROM pe_sleeve_nav")
    con.execute("DELETE FROM pe_waterfall_ledger")
    con.executemany("INSERT INTO pe_sleeve_nav VALUES (?,?,?,?,?,?,?,?,?)", nav_rows)
    con.executemany("INSERT INTO pe_waterfall_ledger (sleeve_id, dist_date, deal_id, tier, "
                    "recipient, amount_eur) VALUES (?,?,?,?,?,?)", ledger_rows)
    con.commit()
    con.close()

    print(f"\nStruck PE sleeve NAV for {len(nav_rows)} quarter ends.\n")
    hdr = (f"{'nav_date':<11}{'mark':>12}{'carry_accr':>12}{'carry_paid':>12}"
           f"{'units':>11}{'nav/unit':>10}")
    print(hdr)
    print("-" * len(hdr))
    for r in nav_rows:
        print(f"{r[0]:<11}{r[2]:>12,.0f}{r[4]:>12,.0f}{r[5]:>12,.0f}{r[7]:>11,.1f}{r[8]:>10.2f}")
    return nav_rows


if __name__ == "__main__":
    run()
