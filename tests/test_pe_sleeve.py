"""
Runs the whole pipeline against a scratch database and checks the PE sleeve
output ties out. data/nav.db is not touched.

    python -m unittest discover tests -v
"""

import contextlib
import io
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import generate_data, calculate_nav, pe_sleeve, reconciliation

SCRATCH_DB = os.path.join(ROOT, "data", "_test_nav.db")
MODULES = (generate_data, calculate_nav, pe_sleeve, reconciliation)


class PeSleevePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_db = generate_data.DB_PATH
        for m in MODULES:
            m.DB_PATH = SCRATCH_DB
        with contextlib.redirect_stdout(io.StringIO()):
            generate_data.main()
            calculate_nav.run()
            cls.findings = reconciliation.run()
        cls.con = sqlite3.connect(SCRATCH_DB)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        for m in MODULES:
            m.DB_PATH = cls.real_db
        os.remove(SCRATCH_DB)

    def test_no_sleeve_breaks(self):
        self.assertEqual([f for f in self.findings if f[0] == "PE_SLEEVE_BREAK"], [])

    def test_liquid_book_report_is_unchanged(self):
        self.assertEqual(len(self.findings), 10)

    def test_first_call_is_priced_at_launch_nav(self):
        nav, units = self.con.execute(
            "SELECT nav_per_unit_eur, units_outstanding FROM pe_sleeve_nav ORDER BY nav_date LIMIT 1"
        ).fetchone()
        self.assertEqual(nav, 100.0)
        self.assertAlmostEqual(units, 20_000.0)

    def test_distributions_leave_units_alone(self):
        rows = self.con.execute(
            "SELECT nav_date, units_outstanding FROM pe_sleeve_nav ORDER BY nav_date").fetchall()
        dist_dates = {d for (d,) in self.con.execute("SELECT dist_date FROM pe_distributions")}
        for (_, before), (d, after) in zip(rows, rows[1:]):
            if d in dist_dates:
                self.assertAlmostEqual(before, after)

    def test_ledger_adds_up_to_each_distribution(self):
        for dist_date, amount in self.con.execute("SELECT dist_date, amount_eur FROM pe_distributions"):
            tiered = self.con.execute(
                "SELECT SUM(amount_eur) FROM pe_waterfall_ledger WHERE dist_date = ?", (dist_date,)
            ).fetchone()[0]
            self.assertAlmostEqual(tiered, amount, places=2)

    def test_a_tampered_carry_figure_is_caught(self):
        self.con.execute("SAVEPOINT tamper")
        self.con.execute("UPDATE pe_sleeve_nav SET carry_accrued_eur = carry_accrued_eur + 5000 "
                         "WHERE nav_date = '2026-06-30'")
        found = reconciliation.check_pe_sleeve(self.con)
        self.con.execute("ROLLBACK TO tamper")
        self.assertTrue(any("fresh waterfall run" in f[3] for f in found))


if __name__ == "__main__":
    unittest.main()
