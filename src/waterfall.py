"""
PE-style incentive fee waterfall: return of capital, preferred return (hurdle),
GP catch-up, then a carried-interest split of remaining profit.

    from src.waterfall import run_waterfall

This is the calculation a hybrid fund uses instead of a hedge-fund-style
performance fee (X% of NAV appreciation above a high-water mark). Where a
hedge fund crystallizes a fee on NAV, a PE-style vehicle runs every dollar
that moves between the LPs and the GP through an ordered set of tiers:

  1. RETURN_OF_CAPITAL    LPs get their called capital back first, 100% to LP
  2. PREFERRED_RETURN      LPs then get a hurdle return on that capital
                           (compounded, actual/365), 100% to LP
  3. GP_CATCHUP            the GP "catches up" until its cumulative share of
                           (preferred return + catch-up) equals carry_pct
  4. CARRY_SPLIT           everything after that splits carry_pct / (1-carry_pct)
                           between GP and LP, uncapped

Two pooling modes, which is the actual textbook distinction between them:

  EUROPEAN (whole-fund)   every capital call and every distribution, across
                          the entire fund's life, shares ONE pool of tiers.
                          The GP cannot reach catch-up/carry until ALL called
                          capital plus its preferred return has been returned,
                          fund-wide. LP-friendly: a loss on a late deal can
                          never be masked by carry already paid on an early
                          winner.

  AMERICAN (deal-by-deal) each investment is its own cohort with its own
                          return-of-capital/preferred-return/catch-up/carry
                          tiers, tracked by tagging cashflows with a `deal_id`.
                          A single profitable cohort can pay the GP carry
                          while another cohort is still underwater. GP-
                          friendly, and standard LPAs pair it with a
                          clawback: if the GP is ultimately overpaid relative
                          to what a whole-fund calculation would have
                          entitled it to, it owes the excess back. See
                          `clawback()`.

Both modes are driven by the same `_Pool` tier engine (ROC -> pref ->
catch-up -> carry); the only difference is whether one pool sees every
cashflow (EUROPEAN) or each deal gets its own pool (AMERICAN).

Left out on purpose, matching the rest of this repo's "things left out on
purpose" style: multiple GPs/co-investors, tax distributions and gross-ups,
and management-fee offsets against carry.
"""

from dataclasses import dataclass, field
from datetime import date as _date, datetime

TIER_ROC = "RETURN_OF_CAPITAL"
TIER_PREF = "PREFERRED_RETURN"
TIER_CATCHUP_GP = "GP_CATCHUP_GP"
TIER_CATCHUP_LP = "GP_CATCHUP_LP"
TIER_CARRY_GP = "CARRY_SPLIT_GP"
TIER_CARRY_LP = "CARRY_SPLIT_LP"

GP_TIERS = (TIER_CATCHUP_GP, TIER_CARRY_GP)
LP_TIERS = (TIER_ROC, TIER_PREF, TIER_CATCHUP_LP, TIER_CARRY_LP)

DEFAULT_DEAL = "_fund"  # the single pool used by EUROPEAN mode


def _to_date(d):
    if isinstance(d, _date):
        return d
    return datetime.strptime(d, "%Y-%m-%d").date()


@dataclass
class LedgerRow:
    event_date: str
    tier: str
    amount: float
    recipient: str            # 'LP' | 'GP'
    deal_id: str = DEFAULT_DEAL
    synthetic: bool = False   # True for the as-of-today unrealized top-up event


@dataclass
class WaterfallResult:
    ledger: list = field(default_factory=list)

    def totals(self):
        out = {}
        for r in self.ledger:
            out[r.tier] = out.get(r.tier, 0.0) + r.amount
        return out

    def gp_total(self):
        return sum(r.amount for r in self.ledger if r.recipient == "GP")

    def lp_total(self):
        return sum(r.amount for r in self.ledger if r.recipient == "LP")

    def event_split(self, event_date):
        """(lp_amount, gp_amount) for the REAL (non-synthetic) event(s) on this date."""
        lp = sum(r.amount for r in self.ledger
                 if r.event_date == event_date and r.recipient == "LP" and not r.synthetic)
        gp = sum(r.amount for r in self.ledger
                 if r.event_date == event_date and r.recipient == "GP" and not r.synthetic)
        return lp, gp


class _Pool:
    """One waterfall tier stack: return of capital, preferred return, GP
    catch-up, carry split -- with running state so it can be fed events one
    at a time. EUROPEAN mode uses a single `_Pool` for the whole fund;
    AMERICAN mode gives each deal its own `_Pool`.
    """

    def __init__(self, hurdle_rate, catchup_gp_share, carry_pct):
        self.hurdle_rate = hurdle_rate
        self.catchup_gp_share = catchup_gp_share
        self.carry_pct = carry_pct
        self.unreturned_capital = 0.0
        self.accrued_pref = 0.0
        self.pref_paid_cum = 0.0
        self.catchup_paid_cum = 0.0
        self.last_date = None

    def _accrue_to(self, d):
        if self.last_date is not None and self.unreturned_capital > 0:
            days = (d - self.last_date).days
            if days > 0:
                self.accrued_pref += self.unreturned_capital * (
                    (1 + self.hurdle_rate) ** (days / 365.0) - 1
                )
        self.last_date = d

    def call(self, event_date, amount):
        d = _to_date(event_date)
        self._accrue_to(d)
        self.unreturned_capital += amount

    def distribute(self, event_date, amount, deal_id=DEFAULT_DEAL, synthetic=False):
        """Tier `amount` through ROC -> pref -> catch-up -> carry. Returns the
        LedgerRow list for just this event.
        """
        d = _to_date(event_date)
        self._accrue_to(d)
        rows = []
        remaining = amount

        roc = min(remaining, self.unreturned_capital)
        if roc:
            self.unreturned_capital -= roc
            remaining -= roc
            rows.append(LedgerRow(event_date, TIER_ROC, roc, "LP", deal_id, synthetic))

        pref = min(remaining, self.accrued_pref)
        if pref:
            self.accrued_pref -= pref
            remaining -= pref
            self.pref_paid_cum += pref
            rows.append(LedgerRow(event_date, TIER_PREF, pref, "LP", deal_id, synthetic))

        # GP catch-up: size the total catch-up tranche (LP + GP shares of it)
        # so that once it is fully paid, GP's cumulative take (catch-up only,
        # since ROC/pref are 100% LP) equals carry_pct of cumulative
        # (preferred return + catch-up).
        if remaining > 0 and self.catchup_gp_share > 0:
            cu_target_total = 0.0
            if self.carry_pct < 1.0:
                cu_target_total = (
                    self.carry_pct * self.pref_paid_cum / (1 - self.carry_pct)
                ) / self.catchup_gp_share
            cu_capacity = max(0.0, cu_target_total - self.catchup_paid_cum)
            cu_amount = min(remaining, cu_capacity)
            if cu_amount:
                gp_share = cu_amount * self.catchup_gp_share
                lp_share = cu_amount - gp_share
                self.catchup_paid_cum += cu_amount
                remaining -= cu_amount
                if gp_share:
                    rows.append(LedgerRow(event_date, TIER_CATCHUP_GP, gp_share, "GP", deal_id, synthetic))
                if lp_share:
                    rows.append(LedgerRow(event_date, TIER_CATCHUP_LP, lp_share, "LP", deal_id, synthetic))

        if remaining > 0:
            gp_split = remaining * self.carry_pct
            lp_split = remaining - gp_split
            if gp_split:
                rows.append(LedgerRow(event_date, TIER_CARRY_GP, gp_split, "GP", deal_id, synthetic))
            if lp_split:
                rows.append(LedgerRow(event_date, TIER_CARRY_LP, lp_split, "LP", deal_id, synthetic))

        return rows

    def balance(self):
        """Outstanding claim (unreturned capital + accrued pref) not yet paid."""
        return self.unreturned_capital + self.accrued_pref


def _normalize(cashflows):
    """Accept (date, type, amount) or (date, type, amount, deal_id) and
    return a chronologically sorted list of (date, type, amount, deal_id).
    Calls are placed before distributions on the same date.
    """
    out = []
    for cf in cashflows:
        if len(cf) == 3:
            d, t, amt = cf
            out.append((d, t, amt, None))
        else:
            out.append(cf)
    out.sort(key=lambda e: (_to_date(e[0]), 0 if e[1] == "CALL" else 1))
    return out


def run_waterfall(cashflows, hurdle_rate, catchup_gp_share, carry_pct,
                   mode="EUROPEAN", as_of_date=None, unrealized_value=0.0):
    """cashflows: iterable of (date, 'CALL'|'DIST', amount) or
    (date, 'CALL'|'DIST', amount, deal_id) with amount > 0.

    EUROPEAN pools every cashflow into one tier stack regardless of deal_id.

    AMERICAN gives each distinct deal_id its own tier stack. A CALL with no
    deal_id seeds its own single-call cohort (deal_id defaults to a call
    index). A DIST with no deal_id is split pro rata across every cohort
    that is live (called on or before that date) by outstanding balance,
    which is the sensible default when the caller has not tagged which
    investment a distribution came from.

    `unrealized_value`, if given with `as_of_date`, is appended as one final
    synthetic distribution -- "what the GP would be entitled to if the fund
    liquidated at today's mark" -- which is how calculate_nav.py accrues a
    mark-to-market carry provision day over day without waiting for a real
    distribution event. In AMERICAN mode it is allocated pro rata like any
    other untagged distribution.
    """
    events = _normalize(cashflows)

    if mode == "EUROPEAN":
        pool = _Pool(hurdle_rate, catchup_gp_share, carry_pct)
        ledger = []
        for d, t, amt, _deal in events:
            if t == "CALL":
                pool.call(d, amt)
            else:
                ledger.extend(pool.distribute(d, amt))
        if unrealized_value and as_of_date is not None:
            ledger.extend(pool.distribute(as_of_date, unrealized_value, synthetic=True))
        return WaterfallResult(ledger)

    if mode != "AMERICAN":
        raise ValueError(f"unknown waterfall mode: {mode!r}")

    return WaterfallResult(_run_american(events, hurdle_rate, catchup_gp_share, carry_pct,
                                          as_of_date, unrealized_value))


def _run_american(events, hurdle_rate, catchup_gp_share, carry_pct, as_of_date, unrealized_value):
    pools = {}          # deal_id -> _Pool
    anon_call_idx = 0

    def pool_for(deal_id):
        if deal_id not in pools:
            pools[deal_id] = _Pool(hurdle_rate, catchup_gp_share, carry_pct)
        return pools[deal_id]

    ledger = []

    def apply_distribution(d, amt, deal_id, synthetic=False):
        if deal_id is not None:
            ledger.extend(pool_for(deal_id).distribute(d, amt, deal_id, synthetic))
            return
        # Untagged: split pro rata across live cohorts by outstanding balance.
        live_ids = [did for did, p in pools.items()]
        if not live_ids:
            return
        # Accrue every live pool to this date first so balances are comparable.
        for did in live_ids:
            pools[did]._accrue_to(_to_date(d))
        balances = {did: pools[did].balance() for did in live_ids}
        pool_total = sum(b for b in balances.values() if b > 0)
        remaining = amt
        for did in live_ids:
            bal = balances[did]
            if pool_total > 0:
                share = bal / pool_total * amt
            else:
                share = amt / len(live_ids)
            share = min(share, remaining)
            remaining -= share
            if share > 0:
                ledger.extend(pools[did].distribute(d, share, did, synthetic))

    for d, t, amt, deal_id in events:
        if t == "CALL":
            if deal_id is None:
                anon_call_idx += 1
                deal_id = f"_call_{anon_call_idx}"
            pool_for(deal_id).call(d, amt)
        else:
            apply_distribution(d, amt, deal_id)

    if unrealized_value and as_of_date is not None:
        apply_distribution(as_of_date, unrealized_value, None, synthetic=True)

    ledger.sort(key=lambda r: (_to_date(r.event_date), r.deal_id, r.tier))
    return ledger


def clawback(cashflows, hurdle_rate, catchup_gp_share, carry_pct, as_of_date, unrealized_value=0.0):
    """How much the GP would owe LPs back under an AMERICAN (deal-by-deal)
    waterfall, measured against what a EUROPEAN (whole-fund) waterfall over
    the identical cashflows would have entitled it to as of the same date.

    This is the real reason American waterfalls carry a clawback clause: a
    profitable early deal can pay the GP carry while a later deal is still
    underwater, so cumulative GP-paid-to-date can run ahead of what the GP is
    truly owed once the whole fund is looked at together. Returns 0.0 if the
    GP has not been overpaid.
    """
    american = run_waterfall(cashflows, hurdle_rate, catchup_gp_share, carry_pct,
                              mode="AMERICAN", as_of_date=as_of_date, unrealized_value=unrealized_value)
    european = run_waterfall(cashflows, hurdle_rate, catchup_gp_share, carry_pct,
                              mode="EUROPEAN", as_of_date=as_of_date, unrealized_value=unrealized_value)
    excess = american.gp_total() - european.gp_total()
    return max(0.0, excess)
