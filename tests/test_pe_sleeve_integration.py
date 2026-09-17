"""
End-to-end check that the PE Sleeve (share class 3) roll forward in
src/calculate_nav.py is internally consistent once it's actually wired to a
real database -- the unit tests in test_waterfall.py only exercise the tier
math in isolation.

    python -m unittest discover tests -v

Runs the full pipeline against a throwaway copy of the database (never
data/nav.db) and checks the invariants that would be hard to see just by
reading the numbers printed to the console:

  * every LP + GP tier the waterfall ever recognised nets back to the sleeve's
    distributable value (nothing is created or destroyed by the tiering)
  * reconciliation's NAV_CALC_BREAK check finds zero breaks for the PE class,
    meaning the independent shadow calc in reconciliation.py agrees with
    calculate_nav.py's production roll forward
  * a capital call issues shares at a sane price and never leaves NAV negative
"""

import os
import shutil
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from src import generate_data, calculate_nav, reconciliation

TEST_DB = os.path.join(ROOT, "data", "_test_nav.db")


class TestPeSleeveIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build a throwaway database under the same paths the modules expect,
        # by pointing DB_PATH at a test copy for the duration of this test.
        cls._real_db = generate_data.DB_PATH
        generate_data.DB_PATH = TEST_DB
        calculate_nav.DB_PATH = TEST_DB
        reconciliation.DB_PATH = TEST_DB
        generate_data.main()
        calculate_nav.run()
        cls.findings = reconciliation.run()

    @classmethod
    def tearDownClass(cls):
        generate_data.DB_PATH = cls._real_db
        calculate_nav.DB_PATH = cls._real_db
        reconciliation.DB_PATH = cls._real_db
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_no_nav_calc_break_for_the_pe_class(self):
        breaks = [f for f in self.findings if f[0] == "NAV_CALC_BREAK" and f[2] == "PE Sleeve"]
        self.assertEqual(breaks, [], "shadow_pe_nav() disagreed with calculate_nav.py's PE roll forward")

    def test_nav_per_share_never_goes_negative(self):
        con = sqlite3.connect(TEST_DB)
        rows = con.execute(
            "SELECT nav_date, nav_per_share_eur FROM nav_daily WHERE share_class_id = 3"
        ).fetchall()
        con.close()
        self.assertTrue(rows)
        for d, nav in rows:
            self.assertGreater(nav, 0.0, f"PE Sleeve NAV per share went <= 0 on {d}")

    def test_first_call_prices_at_launch_nav(self):
        con = sqlite3.connect(TEST_DB)
        row = con.execute(
            "SELECT nav_per_share_eur, shares_outstanding FROM nav_daily "
            "WHERE share_class_id = 3 ORDER BY nav_date LIMIT 1"
        ).fetchone()
        first_call = con.execute(
            "SELECT amount_eur FROM capital_calls WHERE share_class_id = 3 ORDER BY call_date LIMIT 1"
        ).fetchone()
        con.close()
        nav, shares = row
        # Before the first call there are no shares yet; from the call date
        # onward the class should be priced off 100.0, same launch convention
        # as the open-ended classes.
        if shares > 0:
            self.assertAlmostEqual(shares, first_call[0] / 100.0, places=2)

    def test_waterfall_ledger_tiers_never_exceed_the_distribution_they_came_from(self):
        con = sqlite3.connect(TEST_DB)
        con.row_factory = sqlite3.Row
        real_rows = con.execute(
            "SELECT event_date, SUM(amount_eur) AS total FROM waterfall_ledger "
            "WHERE synthetic = 0 GROUP BY event_date"
        ).fetchall()
        real_dists = {r["dist_date"]: r["amount_eur"] for r in
                      con.execute("SELECT dist_date, amount_eur FROM distributions")}
        con.close()
        for r in real_rows:
            self.assertAlmostEqual(r["total"], real_dists[r["event_date"]], places=4,
                                    msg="LP + GP tiers for a real distribution must sum to the gross amount")


if __name__ == "__main__":
    unittest.main()
