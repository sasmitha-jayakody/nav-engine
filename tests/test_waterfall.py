"""
Tests for src/waterfall.py. No database needed.

    python -m unittest discover tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.waterfall import run_waterfall, clawback, ROC, PREF, CATCHUP, CARRY

HURDLE, FULL_CATCHUP, CARRY_PCT = 0.08, 1.0, 0.20


def tier(cashflows, **kw):
    return run_waterfall(cashflows, HURDLE, kw.pop("catchup", FULL_CATCHUP), CARRY_PCT, **kw)


class ReturnOfCapital(unittest.TestCase):
    def test_same_day_round_trip_is_all_capital(self):
        r = tier([("2024-01-01", "CALL", 100.0), ("2024-01-01", "DIST", 100.0)])
        self.assertAlmostEqual(r.total(ROC), 100.0)
        self.assertEqual(r.gp_total(), 0.0)

    def test_a_loss_pays_no_hurdle_and_no_carry(self):
        r = tier([("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 60.0)])
        self.assertAlmostEqual(r.total(ROC), 60.0)
        self.assertEqual(r.total(PREF), 0.0)
        self.assertEqual(r.gp_total(), 0.0)

    def test_two_calls_both_come_back_before_any_profit(self):
        r = tier([("2024-01-01", "CALL", 100.0), ("2024-06-01", "CALL", 50.0),
                  ("2025-01-01", "DIST", 150.0)])
        self.assertAlmostEqual(r.total(ROC), 150.0)
        self.assertEqual(r.gp_total(), 0.0)


class PreferredReturn(unittest.TestCase):
    def test_one_year_at_exactly_the_hurdle(self):
        r = tier([("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 108.0)])
        self.assertAlmostEqual(r.total(PREF), 8.0, places=2)
        self.assertEqual(r.gp_total(), 0.0)


class CatchUpAndCarry(unittest.TestCase):
    def test_full_catch_up_leaves_gp_with_20pct_of_profit(self):
        r = tier([("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 1000.0)])
        self.assertAlmostEqual(r.gp_total() / 900.0, CARRY_PCT)
        self.assertAlmostEqual(r.gp_total() + r.lp_total(), 1000.0)

    def test_half_catch_up_gives_lps_part_of_the_slice(self):
        r = tier([("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 1000.0)], catchup=0.5)
        self.assertGreater(r.total(CATCHUP, "LP"), 0.0)
        self.assertAlmostEqual(r.gp_total() + r.lp_total(), 1000.0)


class HypotheticalLiquidation(unittest.TestCase):
    def test_unrealized_value_accrues_carry_without_a_real_distribution(self):
        r = tier([("2024-01-01", "CALL", 100.0)], as_of_date="2025-01-01", unrealized_value=1000.0)
        self.assertGreater(r.gp_total(), 0.0)
        self.assertTrue(all(row.as_of for row in r.ledger))

    def test_split_on_ignores_the_as_of_rows(self):
        r = tier([("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 200.0)],
                 as_of_date="2025-01-01", unrealized_value=50.0)
        lp, gp = r.split_on("2025-01-01")
        self.assertAlmostEqual(lp + gp, 200.0)


class AmericanVersusEuropean(unittest.TestCase):
    # Deal A is sold at a loss, deal B at a big gain.
    FLOWS = [
        ("2024-01-01", "CALL", 100.0, "A"),
        ("2024-01-01", "CALL", 100.0, "B"),
        ("2025-01-01", "DIST", 60.0, "A"),
        ("2025-01-01", "DIST", 300.0, "B"),
    ]

    def test_one_deal_gives_the_same_answer_either_way(self):
        flows = [("2024-01-01", "CALL", 100.0, "A"), ("2025-01-01", "DIST", 1000.0, "A")]
        self.assertAlmostEqual(tier(flows).gp_total(), tier(flows, mode="AMERICAN").gp_total())
        self.assertAlmostEqual(clawback(flows, HURDLE, FULL_CATCHUP, CARRY_PCT, "2025-01-01"), 0.0)

    def test_american_pays_carry_on_the_winner_despite_the_loser(self):
        european = tier(self.FLOWS)
        american = tier(self.FLOWS, mode="AMERICAN")
        self.assertGreater(american.gp_total(), european.gp_total())
        owed = clawback(self.FLOWS, HURDLE, FULL_CATCHUP, CARRY_PCT, "2025-01-01")
        self.assertAlmostEqual(owed, american.gp_total() - european.gp_total())

    def test_distribution_for_a_deal_with_no_capital_is_rejected(self):
        with self.assertRaises(ValueError):
            tier([("2024-01-01", "CALL", 100.0, "A"), ("2025-01-01", "DIST", 50.0, "Z")],
                 mode="AMERICAN")


if __name__ == "__main__":
    unittest.main()
