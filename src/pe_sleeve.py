"""
Quarterly NAV for the PE sleeve.

    python src/pe_sleeve.py     (needs data/nav.db from generate_data.py)

The sleeve is valued once a quarter. On each valuation date:

  1. take the manager's mark, before the day's calls and distributions
  2. charge the management fee for the quarter on that mark
  3. run the waterfall over every call and distribution so far, with what is
     left of the mark treated as sold today. The GP's total is carry accrued.
  4. work out net assets: mark, less fee, less carry accrued but not paid
  5. deal. A call issues units at net assets per unit before the call. A
     distribution pays out cash and leaves the units alone, so NAV per unit
     falls. The GP's share of the cash is recorded as carry paid.
  6. report NAV per unit after dealing, plus DPI and TVPI

This is a closed-end sleeve that keeps units, so it can report a NAV per unit
the same way the liquid fund does.
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


def multiples(nav_rows, calls, dists):
    """DPI and TVPI to each date, on called capital (paid-in).

    DPI is cash LPs have had back. TVPI adds what they still hold at NAV.
    """
    out = []
    for r in nav_rows:
        d = r[0]
        paid_in = sum(c["amount_eur"] for c in calls if c["call_date"] <= d)
        lp_back = sum(x["lp_eur"] for x in dists if x["dist_date"] <= d)
        if paid_in == 0:
            out.append((0.0, 0.0))
        else:
            out.append((lp_back / paid_in, (lp_back + r[6]) / paid_in))
    return out


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
        issue_price = net_before / units if units > 0 else sleeve["launch_nav"]

        lp_cash, gp_cash = result.split_on(d)
        units += call_today / issue_price
        carry_paid += gp_cash
        net_after = net_before + call_today - lp_cash
        nav = net_after / units if units > 0 else sleeve["launch_nav"]

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
    calls = query(con, "SELECT call_date, amount_eur FROM pe_capital_calls")
    dists = query(con, "SELECT dist_date, SUM(amount_eur) AS lp_eur FROM pe_waterfall_ledger "
                       "WHERE recipient = 'LP' GROUP BY dist_date")
    con.close()

    print(f"\nStruck PE sleeve NAV for {len(nav_rows)} quarter ends.\n")
    hdr = (f"{'nav_date':<11}{'mark':>11}{'carry_accr':>11}{'net_assets':>12}"
           f"{'nav/unit':>9}{'dpi':>6}{'tvpi':>6}")
    print(hdr)
    print("-" * len(hdr))
    for r, (dpi, tvpi) in zip(nav_rows, multiples(nav_rows, calls, dists)):
        mark = f"{r[2]:,.0f}" if r[2] else "launch"
        print(f"{r[0]:<11}{mark:>11}{r[4]:>11,.0f}{r[6]:>12,.0f}{r[8]:>9.2f}{dpi:>6.2f}{tvpi:>6.2f}")
    return nav_rows


if __name__ == "__main__":
    run()
