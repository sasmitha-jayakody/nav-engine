"""
Strikes the daily NAV per share class. This is the production side of the
engine, and reconciliation.py is what checks its output.

    python src/calculate_nav.py     (needs data/nav.db from generate_data.py)

Valuing the portfolio happens in SQL, in the v_fund_gav view defined by
sql/02_nav_calculation.sql. What is left is the accounting roll forward, which
lives here because each day depends on the day before:

  for each NAV date and each share class
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
"""

import os
import sqlite3

try:
    from src import pe_sleeve
except ImportError:
    import pe_sleeve

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "nav.db")
VALUATION_SQL = os.path.join(ROOT, "sql", "02_nav_calculation.sql")

LAUNCH_NAV = 100.0                  # NAV per share at inception
CLASS_SPLIT = {1: 0.70, 2: 0.30}    # each class's share of net assets at inception


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


def run():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON;")
    gav, classes, flows, fx, div_by_date = load(con)

    dates = [r["val_date"] for r in gav]
    gav_by_date = {r["val_date"]: r["gross_asset_value_eur"] for r in gav}
    inception, nav_dates = dates[0], dates[1:]

    # ---- initial state at inception --------------------------------------
    gav0 = gav_by_date[inception]
    state = {}  # share_class_id -> dict of running figures
    for c in classes:
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

    # ---- persist ----------------------------------------------------------
    con.execute("DELETE FROM fee_accruals")
    con.execute("DELETE FROM nav_daily")
    con.executemany("INSERT INTO fee_accruals VALUES (?,?,?)", fee_rows)
    con.executemany("INSERT INTO nav_daily VALUES (?,?,?,?,?,?)", nav_rows)

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
    con.close()

    print(f"Struck NAV for {len(nav_dates)} dates x {len(state)} classes.\n")
    hdr = f"{'nav_date':<11}{'class':<10}{'ccy':<5}{'shares':>12}{'nav_eur':>11}{'nav_ccy':>11}"
    print(hdr)
    print("-" * len(hdr))
    for r in out[:4] + out[-4:]:
        print(f"{r['nav_date']:<11}{r['class_name']:<10}{r['currency']:<5}"
              f"{r['shares']:>12}{r['nav_eur']:>11}{r['nav_ccy']:>11}")

    pe_sleeve.run()  # the PE sleeve has its own book and quarterly NAV
    return out


if __name__ == "__main__":
    run()
