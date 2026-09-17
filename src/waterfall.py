"""
Distribution waterfall for the PE sleeve.

Cash going back to investors is split in tiers, in this order:

  RETURN_OF_CAPITAL  LPs get called capital back. All to LP.
  PREFERRED_RETURN   LPs get the hurdle on that capital, compounded
                     actual/365. All to LP.
  CATCHUP            The GP takes catchup_gp_share of the next slice until
                     it holds carry_pct of the profit paid so far.
  CARRY              The rest splits carry_pct to the GP, the remainder to LPs.

EUROPEAN runs every cashflow through one set of tiers for the whole sleeve.
AMERICAN keeps a set of tiers per deal (cashflows tagged with a deal_id), so
a winning deal can pay carry while another deal is still under water.
clawback() measures how far AMERICAN has overpaid the GP compared with
EUROPEAN on the same cashflows.

Not modelled: fee offsets, tax distributions, fees counting as contributed
capital, and more than one GP.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

ROC = "RETURN_OF_CAPITAL"
PREF = "PREFERRED_RETURN"
CATCHUP = "CATCHUP"
CARRY = "CARRY"

WHOLE_FUND = "_fund"


def _to_date(d):
    return d if isinstance(d, date) else datetime.strptime(d, "%Y-%m-%d").date()


@dataclass
class LedgerRow:
    event_date: str
    tier: str
    recipient: str        # 'LP' or 'GP'
    amount: float
    deal_id: str = WHOLE_FUND
    as_of: bool = False   # True if this came from the unrealized as-of value


@dataclass
class WaterfallResult:
    ledger: list = field(default_factory=list)

    def gp_total(self):
        return sum(r.amount for r in self.ledger if r.recipient == "GP")

    def lp_total(self):
        return sum(r.amount for r in self.ledger if r.recipient == "LP")

    def total(self, tier, recipient=None):
        return sum(r.amount for r in self.ledger
                   if r.tier == tier and (recipient is None or r.recipient == recipient))

    def split_on(self, event_date):
        """(lp, gp) paid by real distributions on this date."""
        rows = [r for r in self.ledger if r.event_date == event_date and not r.as_of]
        lp = sum(r.amount for r in rows if r.recipient == "LP")
        gp = sum(r.amount for r in rows if r.recipient == "GP")
        return lp, gp


class _Tiers:
    """One set of tiers with running balances, fed one cashflow at a time."""

    def __init__(self, hurdle, catchup_gp_share, carry_pct):
        if 0 < catchup_gp_share <= carry_pct:
            raise ValueError("catch-up share has to be above the carry percentage, "
                             "or the GP can never catch up")
        self.hurdle = hurdle
        self.catchup_gp_share = catchup_gp_share
        self.carry_pct = carry_pct
        self.capital = 0.0       # called and not yet returned
        self.pref_owed = 0.0     # accrued hurdle not yet paid
        self.pref_paid = 0.0
        self.catchup_paid = 0.0  # whole catch-up slice, LP and GP parts
        self.last = None

    def _accrue(self, d):
        if self.last is not None and self.capital > 0:
            days = (d - self.last).days
            self.pref_owed += self.capital * ((1 + self.hurdle) ** (days / 365.0) - 1)
        self.last = d

    def balance(self):
        return self.capital + self.pref_owed

    def call(self, when, amount):
        self._accrue(_to_date(when))
        self.capital += amount

    def distribute(self, when, amount, deal_id=WHOLE_FUND, as_of=False):
        self._accrue(_to_date(when))
        rows, left = [], amount

        def pay(tier, recipient, amt):
            if amt > 0:
                rows.append(LedgerRow(when, tier, recipient, amt, deal_id, as_of))

        roc = min(left, self.capital)
        self.capital -= roc
        left -= roc
        pay(ROC, "LP", roc)

        pref = min(left, self.pref_owed)
        self.pref_owed -= pref
        self.pref_paid += pref
        left -= pref
        pay(PREF, "LP", pref)

        # Size the catch-up slice S so the GP ends it holding carry_pct of all
        # profit paid so far: share * S = carry * (pref + S), which gives
        # S = carry * pref / (share - carry).
        if left > 0 and self.catchup_gp_share > 0:
            target = self.carry_pct * self.pref_paid / (self.catchup_gp_share - self.carry_pct)
            slice_ = min(left, max(0.0, target - self.catchup_paid))
            self.catchup_paid += slice_
            left -= slice_
            pay(CATCHUP, "GP", slice_ * self.catchup_gp_share)
            pay(CATCHUP, "LP", slice_ * (1 - self.catchup_gp_share))

        pay(CARRY, "GP", left * self.carry_pct)
        pay(CARRY, "LP", left * (1 - self.carry_pct))
        return rows


def _sorted(cashflows):
    """Accept (date, kind, amount) or (date, kind, amount, deal_id). Calls sort
    ahead of distributions on the same date."""
    out = [(cf[0], cf[1], cf[2], cf[3] if len(cf) > 3 else None) for cf in cashflows]
    out.sort(key=lambda c: (_to_date(c[0]), c[1] != "CALL"))
    return out


def run_waterfall(cashflows, hurdle, catchup_gp_share, carry_pct,
                  mode="EUROPEAN", as_of_date=None, unrealized_value=0.0):
    """Tier a list of CALL and DIST cashflows.

    If unrealized_value is given, it is tiered last as if it were paid out on
    as_of_date. That is the hypothetical liquidation (HLBV) view the sleeve
    uses to accrue carry between real distributions.
    """
    flows = _sorted(cashflows)
    if mode == "EUROPEAN":
        tiers, ledger = _Tiers(hurdle, catchup_gp_share, carry_pct), []
        for when, kind, amount, deal_id in flows:
            if kind == "CALL":
                tiers.call(when, amount)
            else:
                ledger += tiers.distribute(when, amount, deal_id or WHOLE_FUND)
        if unrealized_value > 0 and as_of_date:
            ledger += tiers.distribute(as_of_date, unrealized_value, as_of=True)
        return WaterfallResult(ledger)
    if mode == "AMERICAN":
        return WaterfallResult(_american(flows, hurdle, catchup_gp_share, carry_pct,
                                         as_of_date, unrealized_value))
    raise ValueError(f"unknown waterfall mode {mode!r}")


def _american(flows, hurdle, catchup_gp_share, carry_pct, as_of_date, unrealized_value):
    deals, ledger, untagged_calls = {}, [], 0

    def deal(deal_id):
        return deals.setdefault(deal_id, _Tiers(hurdle, catchup_gp_share, carry_pct))

    def distribute(when, amount, deal_id, as_of=False):
        if deal_id is not None:
            if deal_id not in deals:
                raise ValueError(f"distribution for {deal_id!r} but no capital was called for it")
            ledger.extend(deals[deal_id].distribute(when, amount, deal_id, as_of))
            return
        # No deal named: share it across deals by what each is still owed.
        for t in deals.values():
            t._accrue(_to_date(when))
        owed = {k: t.balance() for k, t in deals.items()}
        total = sum(owed.values())
        for k, t in deals.items():
            share = amount * owed[k] / total if total > 0 else amount / len(deals)
            ledger.extend(t.distribute(when, share, k, as_of))

    for when, kind, amount, deal_id in flows:
        if kind == "CALL":
            if deal_id is None:
                untagged_calls += 1
                deal_id = f"call_{untagged_calls}"
            deal(deal_id).call(when, amount)
        else:
            distribute(when, amount, deal_id)

    if unrealized_value > 0 and as_of_date and deals:
        distribute(as_of_date, unrealized_value, None, as_of=True)
    return ledger


def clawback(cashflows, hurdle, catchup_gp_share, carry_pct, as_of_date, unrealized_value=0.0):
    """GP carry under AMERICAN minus GP carry under EUROPEAN, floored at zero."""
    kwargs = dict(as_of_date=as_of_date, unrealized_value=unrealized_value)
    american = run_waterfall(cashflows, hurdle, catchup_gp_share, carry_pct, "AMERICAN", **kwargs)
    european = run_waterfall(cashflows, hurdle, catchup_gp_share, carry_pct, "EUROPEAN", **kwargs)
    return max(0.0, american.gp_total() - european.gp_total())
