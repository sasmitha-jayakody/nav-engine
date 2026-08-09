"""
Builds the synthetic dataset everything else runs on.

    python src/generate_data.py

Drops and recreates data/nav.db from sql/01_schema.sql, then fills every table
with about a month of data for a small EUR fund with two share classes.

Five errors are planted on purpose for the reconciliation layer to find. Each
one carries a  # PLANTED ERROR  comment, so you can see what bad data looks like
sitting in the raw tables rather than only in the report.

The random seed is fixed, so every clone builds the same database and produces
the same exceptions report.
"""

import os
import random
import sqlite3
from datetime import date, timedelta

random.seed(42)  # reproducible

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "nav.db")
SCHEMA_PATH = os.path.join(ROOT, "sql", "01_schema.sql")

BASE_CCY = "EUR"


# ---------------------------------------------------------------------------
# Calendar: business days in January 2024. The first day is inception, where the
# fund launches fully invested at 100.00 per share. NAV is struck from day two.
# ---------------------------------------------------------------------------
def business_days(start: date, end: date):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DATES = business_days(date(2024, 1, 1), date(2024, 1, 31))
INCEPTION = DATES[0]
NAV_DATES = DATES[1:]

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
SECURITIES = [
    # id, isin,           ticker,  name,                 asset,    ccy,  exch,   start_price, opening_qty
    (1, "NL0010273215", "ASML",   "ASML Holding NV",     "EQUITY", "EUR", "AEX",    600.0,  4000),
    (2, "DE0007164600", "SAP",    "SAP SE",              "EQUITY", "EUR", "XETRA",  140.0,  9000),
    (3, "US0378331005", "AAPL",   "Apple Inc",           "EQUITY", "USD", "NASDAQ", 185.0, 12000),
    (4, "US5949181045", "MSFT",   "Microsoft Corp",      "EQUITY", "USD", "NASDAQ", 370.0,  5000),
    (5, "CH0038863350", "NESN",   "Nestle SA",           "EQUITY", "CHF", "SIX",    100.0, 15000),
    (6, "DK0062498333", "NOVOB",  "Novo Nordisk A/S",    "EQUITY", "DKK", "CPH",    700.0,  6000),
    (7, "GB00BP6MXD84", "SHEL",   "Shell PLC",           "EQUITY", "GBP", "LSE",     25.0, 40000),
    (8, "US91282CJL55", "UST34",  "US Treasury 4% 2034", "BOND",   "USD", "OTC",     98.0, 20000),
]

SHARE_CLASSES = [
    # id, name,       ccy,   policy, mgmt_fee_bps, inception
    (1, "EUR Acc",  "EUR", "ACC",  75,  INCEPTION),   # institutional-ish
    (2, "USD Dist", "USD", "DIST", 150, INCEPTION),   # retail, higher fee
]

# FX: 1 unit of currency = this many EUR. EUR->EUR is always 1.0.
FX_START = {"EUR": 1.0, "USD": 0.92, "CHF": 1.05, "DKK": 0.134, "GBP": 1.17}


def gen_prices():
    """Random-walk close prices per security per date, then inject the errors."""
    prices = {}  # (date, security_id) -> price
    for s in SECURITIES:
        sid, price = s[0], s[7]
        for d in DATES:
            drift = random.uniform(-0.012, 0.013)  # ~1% daily vol
            price = round(price * (1 + drift), 4)
            prices[(d, sid)] = price

    # -- Correctly booked 2-for-1 split on MSFT (id 4), ex 2024-01-10 ---------
    # Price halves on ex-date and holdings double to match, so fund value is
    # continuous across the split. This is the control case: nothing should flag.
    split_msft_ex = "2024-01-10"
    for d in DATES:
        if d >= split_msft_ex:
            prices[(d, 4)] = round(prices[(d, 4)] / 2.0, 4)

    # -- PLANTED ERROR 1: stale price on SAP (id 2), 2024-01-15..19 -----------
    # One price copied across a run of days, which is what a feed that quietly
    # stopped updating looks like from the database side.
    stale_val = prices[("2024-01-12", 2)]
    for d in ["2024-01-15", "2024-01-16", "2024-01-17", "2024-01-18", "2024-01-19"]:
        prices[(d, 2)] = stale_val  # PLANTED ERROR (stale price)

    # -- PLANTED ERROR 2: fat-finger spike on AAPL (id 3), 2024-01-17 ---------
    # Price keyed in about 10x too high and back to normal the next day. This is
    # the one that cascades into several linked exceptions.
    prices[("2024-01-17", 3)] = round(prices[("2024-01-17", 3)] * 10, 4)  # PLANTED ERROR (price outlier)

    # -- PLANTED ERROR 3: unrecorded split on NOVOB (id 6), ex 2024-01-24 -----
    # A real 2:1 split, so the market price halves, but no corporate action is
    # booked and the holding is never doubled. Fund value drops by half this
    # position with nothing in the data to justify it.
    for d in DATES:
        if d >= "2024-01-24":
            prices[(d, 6)] = round(prices[(d, 6)] / 2.0, 4)  # PLANTED ERROR (missing corp action)

    # -- PLANTED ERROR 4: missing price on NESN (id 5), 2024-01-22 ------------
    # The row is deleted outright, so there is no price at all on that date.
    prices.pop(("2024-01-22", 5), None)  # PLANTED ERROR (missing price)

    return prices


def gen_fx():
    """Random-walk FX rates to EUR per currency per date."""
    fx = {}
    rates = dict(FX_START)
    for d in DATES:
        for ccy, r in list(rates.items()):
            if ccy == "EUR":
                fx[(d, ccy)] = 1.0
                continue
            rates[ccy] = round(r * (1 + random.uniform(-0.004, 0.004)), 6)
            fx[(d, ccy)] = rates[ccy]
    return fx


def gen_holdings():
    """Opening positions carried each day, doubled from the MSFT split ex-date."""
    holdings = {}  # (date, security_id) -> quantity
    opening = {s[0]: float(s[8]) for s in SECURITIES}
    for d in DATES:
        for sid, qty in opening.items():
            # MSFT (id 4) genuine 2:1 split correctly processed: double quantity
            if sid == 4 and d >= "2024-01-10":
                holdings[(d, sid)] = qty * 2.0
            else:
                holdings[(d, sid)] = qty
    return holdings


def gen_corporate_actions():
    """Only the correctly handled actions are booked here.

    The NOVOB 2:1 split on 2024-01-24 is left out on purpose (planted error 3),
    even though the price series clearly reflects it.
    """
    return [
        # ca_id, security_id, type,       ex_date,      pay_date,     amt_per_share, split_ratio
        (1, 4, "SPLIT",    "2024-01-10", "2024-01-10", None, 2.0),   # MSFT 2:1, handled
        (2, 1, "DIVIDEND", "2024-01-12", "2024-01-12", 1.50, None),  # ASML cash dividend, handled
    ]


def gen_transactions():
    """Inception purchases that set up the opening book. Nothing trades after that."""
    txns, tid = [], 1
    for s in SECURITIES:
        sid, start_price, qty = s[0], s[7], s[8]
        txns.append((tid, INCEPTION, sid, "BUY", float(qty), float(start_price)))
        tid += 1
    return txns


def gen_flows():
    """Investor subscriptions / redemptions, in shares, per class per date."""
    return [
        # flow_id, date,         share_class_id, type,  shares
        (1, "2024-01-08", 1, "SUB", 5000.0),
        (2, "2024-01-08", 2, "SUB", 3000.0),
        (3, "2024-01-16", 1, "RED", 2000.0),
        (4, "2024-01-23", 2, "SUB", 4000.0),
        (5, "2024-01-29", 1, "SUB", 1500.0),
    ]


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON;")
    with open(SCHEMA_PATH) as f:
        con.executescript(f.read())

    con.executemany(
        "INSERT INTO security_master VALUES (?,?,?,?,?,?,?)",
        [(s[0], s[1], s[2], s[3], s[4], s[5], s[6]) for s in SECURITIES],
    )
    con.executemany("INSERT INTO share_classes VALUES (?,?,?,?,?,?)", SHARE_CLASSES)

    prices = gen_prices()
    con.executemany(
        "INSERT INTO daily_prices VALUES (?,?,?)",
        [(d, sid, p) for (d, sid), p in sorted(prices.items())],
    )

    fx = gen_fx()
    con.executemany(
        "INSERT INTO fx_rates VALUES (?,?,?)",
        [(d, ccy, r) for (d, ccy), r in sorted(fx.items())],
    )

    holdings = gen_holdings()
    con.executemany(
        "INSERT INTO holdings VALUES (?,?,?)",
        [(d, sid, q) for (d, sid), q in sorted(holdings.items())],
    )

    con.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?)", gen_transactions())
    con.executemany(
        "INSERT INTO corporate_actions VALUES (?,?,?,?,?,?,?)", gen_corporate_actions()
    )
    con.executemany(
        "INSERT INTO subscriptions_redemptions VALUES (?,?,?,?,?)", gen_flows()
    )

    con.commit()
    n_prices = con.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
    con.close()
    print(f"Built {DB_PATH}")
    print(f"  {len(DATES)} business days ({INCEPTION} inception, {len(NAV_DATES)} NAV dates)")
    print(f"  {len(SECURITIES)} securities, {len(SHARE_CLASSES)} share classes, {n_prices} price rows")
    print("  5 errors planted (see the # PLANTED ERROR comments)")


if __name__ == "__main__":
    main()
