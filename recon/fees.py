"""The rate card, and what a payment is expected to net.

Every deduction between a sale and the money landing is **arithmetic**, not judgment:

    net = gross - MDR(method) - GST(18% of the MDR) - TDS(0.1% of gross)

An LLM guessing any of those numbers would be a disqualifier. They are computed here
in closed form from a rate card, in integer paise, with each component rounded exactly
once - because the gateway rounds each component too, and a decomposition that rounds
differently will be out by a few paise on every settlement and balance on none.

**Why this exists at M9 rather than M10.** The global solver needs a target. Given only
a gross-sum tolerance wide enough to absorb any plausible deduction (6%), the CP-SAT
model has an enormous feasible region and returns merely FEASIBLE after ten seconds.
Given each payment's *expected net*, the band collapses to rounding noise, the search
space collapses with it, and the same model proves OPTIMAL in well under a second.
Knowing the rate card is not cheating - a merchant has it in their contract.

M10 turns this around: rather than trusting the card, it **fits** the effective rates
from the batch by robust non-negative least squares and reports the residual, so a
merchant whose real MDR differs from their contract finds out.

The India specifics that a single blended rate cannot express:

* **UPI and RuPay debit carry zero MDR** by regulation. A model with one average rate
  is wrong on more than half of a typical Indian merchant's volume.
* **GST is 18% of the fee, not of the sale.** Charging it on gross overstates the
  deduction by roughly fifty times.
* **TDS under section 194-O is 0.1% of gross**, not of the fee, and applies whether or
  not any MDR was charged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from core.money import Money, Rate
from ingest.canonical import PaymentMethod

#: Default MDR in basis points, by method. These are plausible Indian card-not-present
#: rates and are the *starting* card, not a claim about any specific merchant's
#: contract - M10 fits the effective rates from the data and reports the difference.
DEFAULT_MDR_BPS: Final[dict[PaymentMethod, int]] = {
    PaymentMethod.UPI: 0,
    PaymentMethod.RUPAY_DEBIT: 0,
    PaymentMethod.BANK_TRANSFER: 0,
    PaymentMethod.CARD_DEBIT: 90,
    PaymentMethod.CARD_CREDIT: 200,
    PaymentMethod.NETBANKING: 175,
    PaymentMethod.WALLET: 220,
    PaymentMethod.EMI: 300,
    PaymentMethod.INTERNATIONAL_CARD: 430,
    PaymentMethod.UNKNOWN: 200,
}

GST_ON_FEE_PERCENT: Final = 18
TDS_194O_BPS: Final = 10


@dataclass(frozen=True, slots=True)
class Deduction:
    """One named component of the difference between gross and net."""

    component: str
    amount: Money
    basis: str
    """What the rate was applied to - the phrase that makes the waterfall checkable."""

    rate: str = ""


@dataclass(frozen=True, slots=True)
class Breakdown:
    """A single payment's gross, its deductions, and the net that should land."""

    gross: Money
    deductions: tuple[Deduction, ...]
    net: Money

    def total_deducted(self) -> Money:
        total = Money.zero(self.gross.currency)
        for deduction in self.deductions:
            total = total + deduction.amount
        return total

    def balances(self) -> bool:
        """gross - deductions == net, to the paise. Asserted, not assumed."""
        return self.gross.paise - self.total_deducted().paise == self.net.paise


@dataclass(frozen=True, slots=True)
class RateCard:
    """What the merchant is charged. Configurable; fitted and checked at M10."""

    mdr_bps: dict[PaymentMethod, int] = field(
        default_factory=lambda: dict(DEFAULT_MDR_BPS)
    )
    gst_percent: int = GST_ON_FEE_PERCENT
    tds_bps: int = TDS_194O_BPS

    def mdr(self, method: PaymentMethod) -> Rate:
        return Rate.from_bps(self.mdr_bps.get(method, DEFAULT_MDR_BPS[PaymentMethod.UNKNOWN]),
                             label=f"MDR {method.value}")

    @property
    def gst(self) -> Rate:
        return Rate.from_percent(self.gst_percent, label="GST on fee")

    @property
    def tds(self) -> Rate:
        return Rate.from_bps(self.tds_bps, label="TDS 194-O")

    def decompose(self, gross: Money, method: PaymentMethod) -> Breakdown:
        """Gross to net, in closed form, each component rounded exactly once."""
        mdr_amount = self.mdr(method).of(gross)
        gst_amount = self.gst.of(mdr_amount)
        tds_amount = self.tds.of(gross)
        deductions = (
            Deduction(
                "fee_mdr",
                mdr_amount,
                f"{self.mdr_bps.get(method, 200)} bps of gross",
                str(self.mdr(method)),
            ),
            Deduction(
                "gst_on_fee",
                gst_amount,
                f"{self.gst_percent}% of the MDR (not of the sale)",
                str(self.gst),
            ),
            Deduction(
                "tds_194o",
                tds_amount,
                f"{self.tds_bps} bps of gross under section 194-O",
                str(self.tds),
            ),
        )
        net = Money(
            gross.paise - mdr_amount.paise - gst_amount.paise - tds_amount.paise,
            gross.currency,
        )
        breakdown = Breakdown(gross=gross, deductions=deductions, net=net)
        if not breakdown.balances():
            raise AssertionError("fee decomposition does not balance - arithmetic bug")
        return breakdown

    def expected_net(self, gross: Money, method: PaymentMethod) -> Money:
        """Just the net. The target the subset solver aims at."""
        return self.decompose(gross, method).net

    def canonical(self) -> dict[str, object]:
        return {
            "mdr_bps": {m.value: bps for m, bps in sorted(self.mdr_bps.items())},
            "gst_percent": self.gst_percent,
            "tds_bps": self.tds_bps,
        }

    def describe(self) -> str:
        lines = [f"GST {self.gst_percent}% on the fee · TDS {self.tds_bps} bps on gross", "MDR:"]
        for method, bps in sorted(self.mdr_bps.items(), key=lambda item: item[0].value):
            note = "  (zero-rated by regulation)" if bps == 0 else ""
            lines.append(f"  {method.value:20} {bps:4} bps{note}")
        return "\n".join(lines)


DEFAULT_RATE_CARD: Final = RateCard()


def effective_deduction_rate(gross: Money, method: PaymentMethod,
                             card: RateCard = DEFAULT_RATE_CARD) -> Decimal:
    """Total share of gross withheld, for sizing a solver tolerance."""
    if gross.paise == 0:
        return Decimal(0)
    breakdown = card.decompose(gross, method)
    return Decimal(breakdown.total_deducted().paise) / Decimal(gross.paise)
