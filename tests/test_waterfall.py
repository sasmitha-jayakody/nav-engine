"""
Unit tests for src/waterfall.py.

    python -m unittest discover tests -v

No database involved -- this exercises the tier math on its own, which is
where a waterfall bug actually lives (the calculate_nav.py wiring is checked
separately, end to end, by run.py + reconciliation.py's NAV_CALC_BREAK check).
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from src.waterfall import run_waterfall, clawback, TIER_ROC, TIER_PREF, TIER_CATCHUP_GP, TIER_CARRY_GP


HURDLE = 0.08
CATCHUP = 1.0     # 100% GP catch-up
CARRY = 0.20      # 20% carried interest


def approx(a, b, tol=1e-6):
    return abs(a - b) < tol


class TestReturnOfCapitalOnly(unittest.TestCase):
    def test_break_even_same_day_is_pure_roc(self):
        cashflows = [("2024-01-01", "CALL", 100.0), ("2024-01-01", "DIST", 100.0)]
        res = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN")
        totals = res.totals()
        self.assertTrue(approx(totals.get(TIER_ROC, 0.0), 100.0))
        self.assertEqual(res.gp_total(), 0.0)

    def test_loss_returns_less_than_capital_no_pref_no_carry(self):
        cashflows = [("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 60.0)]
        res = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN")
        totals = res.totals()
        self.assertTrue(approx(totals.get(TIER_ROC, 0.0), 60.0))
        self.assertNotIn(TIER_PREF, totals)
        self.assertEqual(res.gp_total(), 0.0)


class TestPreferredReturn(unittest.TestCase):
    def test_exact_hurdle_after_one_year_no_catchup(self):
        cashflows = [("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 108.0)]
        res = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN")
        totals = res.totals()
        self.assertTrue(approx(totals.get(TIER_ROC, 0.0), 100.0))
        self.assertTrue(approx(totals.get(TIER_PREF, 0.0), 8.0, tol=1e-3))
        self.assertEqual(res.gp_total(), 0.0)


class TestCatchupAndCarrySplit(unittest.TestCase):
    def test_gp_ends_up_with_exactly_carry_pct_of_profit(self):
        # 100% catch-up: once GP reaches carry_pct of (pref + catch-up), the
        # residual splits carry_pct/(1-carry_pct). GP's share of TOTAL PROFIT
        # (everything above return of capital) should land on carry_pct.
        cashflows = [("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 1000.0)]
        res = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN")
        profit = 1000.0 - 100.0
        self.assertTrue(approx(res.gp_total() / profit, CARRY, tol=1e-6))
        self.assertTrue(approx(res.gp_total() + res.lp_total(), 1000.0))

    def test_partial_catchup_share_leaves_lp_with_some_of_the_catchup_tranche(self):
        cashflows = [("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 1000.0)]
        res = run_waterfall(cashflows, HURDLE, 0.5, CARRY, mode="EUROPEAN")
        totals = res.totals()
        self.assertIn("GP_CATCHUP_LP", totals)
        # GP no longer reaches exactly carry_pct as fast, but total still nets out.
        self.assertTrue(approx(res.gp_total() + res.lp_total(), 1000.0))


class TestMultipleCalls(unittest.TestCase):
    def test_second_call_adds_its_own_principal_to_the_pool(self):
        cashflows = [
            ("2024-01-01", "CALL", 100.0),
            ("2024-06-01", "CALL", 50.0),
            ("2025-01-01", "DIST", 150.0),
        ]
        res = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN")
        totals = res.totals()
        self.assertTrue(approx(totals.get(TIER_ROC, 0.0), 150.0))
        self.assertEqual(res.gp_total(), 0.0)  # exactly capital back, no profit


class TestMarkToMarketAccrual(unittest.TestCase):
    def test_unrealized_value_produces_a_synthetic_gp_accrual(self):
        cashflows = [("2024-01-01", "CALL", 100.0)]
        res = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN",
                             as_of_date="2025-01-01", unrealized_value=1000.0)
        self.assertGreater(res.gp_total(), 0.0)
        for row in res.ledger:
            self.assertTrue(row.synthetic)

    def test_event_split_only_counts_real_events(self):
        cashflows = [
            ("2024-01-01", "CALL", 100.0),
            ("2025-01-01", "DIST", 200.0),
        ]
        res = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN",
                             as_of_date="2025-01-01", unrealized_value=50.0)
        lp, gp = res.event_split("2025-01-01")
        self.assertTrue(approx(lp + gp, 200.0))


class TestAmericanVsEuropeanAndClawback(unittest.TestCase):
    def test_single_cohort_american_equals_european(self):
        # With only one call, deal-by-deal and whole-fund pooling coincide.
        cashflows = [("2024-01-01", "CALL", 100.0), ("2025-01-01", "DIST", 1000.0)]
        eur = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN")
        usa = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="AMERICAN")
        self.assertTrue(approx(eur.gp_total(), usa.gp_total(), tol=1e-6))
        cb = clawback(cashflows, HURDLE, CATCHUP, CARRY, as_of_date="2025-01-01")
        self.assertTrue(approx(cb, 0.0))

    def test_profitable_early_cohort_can_overpay_gp_under_american(self):
        # Deal A: called early, realized (distributed) at a big profit soon
        # after. Deal B: called at the same time, still sitting at a loss as
        # of the test date. American ties the distribution to deal A alone,
        # so it pays GP carry on deal A's profit right away; European pools
        # both calls together, so deal A's gain first has to cover deal B's
        # unrealized loss before any carry is owed. American-paid GP carry
        # should therefore run ahead of European entitlement -- exactly the
        # scenario clawback provisions exist for.
        cashflows = [
            ("2024-01-01", "CALL", 100.0, "deal_A"),
            ("2024-01-01", "CALL", 100.0, "deal_B"),
            ("2024-02-01", "DIST", 1000.0, "deal_A"),  # deal A realized at a big gain
        ]
        as_of = "2024-06-01"
        unrealized = 40.0  # deal B currently marked well below its 100 cost

        usa = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="AMERICAN",
                             as_of_date=as_of, unrealized_value=unrealized)
        eur = run_waterfall(cashflows, HURDLE, CATCHUP, CARRY, mode="EUROPEAN",
                             as_of_date=as_of, unrealized_value=unrealized)
        self.assertGreater(usa.gp_total(), eur.gp_total())

        cb = clawback(cashflows, HURDLE, CATCHUP, CARRY, as_of_date=as_of, unrealized_value=unrealized)
        self.assertTrue(approx(cb, usa.gp_total() - eur.gp_total()))
        self.assertGreater(cb, 0.0)


if __name__ == "__main__":
    unittest.main()
