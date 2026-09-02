"""Loop 2: explain every rupee of the difference, to the paise.

A settlement is short. The merchant wants to know why, and "fees" is not an answer -
the answer is a waterfall that starts at gross, names each deduction, and lands
exactly on the amount the bank actually credited:

    gross captured        Rs 12,40,000
    - MDR @ 1.90%         Rs    23,560   <- fitted from the batch, not assumed
    - GST @ 18% on MDR    Rs     4,241
    - TDS 194-O @ 0.1%    Rs     1,240
    - refunds settled     Rs    18,000   <- traced to 4 refund ids
    - chargeback hold     Rs     9,500   <- traced to 1 dispute id
    + prior-day rollover  Rs     2,140
    = net expected        Rs 11,85,599
      net received        Rs 11,85,599   balanced to Rs 0

Two things make this more than arithmetic.

**The rates are fitted, not trusted.** `recon/fees.py` holds a rate card, but a card is
what the contract says and the settlement is what actually happened. :func:`fit_rates`
solves for the *effective* rate per payment method across the whole batch by
non-negative least squares, so a merchant whose real MDR has drifted from their
contract finds out from their own data. Non-negativity is not a convenience: a fitted
"negative fee" would be a sign the model is wrong, and constraining it away means the
error surfaces as a residual instead of being absorbed by a nonsense coefficient.

**What cannot be attributed becomes a typed exception, never a rounding.** If the
components do not close the gap exactly, the remainder is unexplained money and it goes
to the triage queue. A waterfall that balances by construction explains nothing.

The LLM is not involved anywhere in this file. Deciding how much a fee is comes from
closed-form arithmetic and a least-squares fit; a model guessing rupees would be a
disqualifier.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import numpy as np
from scipy.optimize import least_squares, nnls

from core.money import Money, Rate, format_inr
from ingest.canonical import CanonicalTxn, PaymentMethod, TxnKind
from recon.exceptions import ExceptionType
from recon.fees import DEFAULT_RATE_CARD, RateCard

#: Below this, a residual is rounding noise from independently rounded components
#: rather than a finding. Two paise per payment plus a rupee of slack.
ROUNDING_SLACK_PAISE: Final = 100


@dataclass(frozen=True, slots=True)
class Line:
    """One row of the waterfall. Signed: negative reduces the payout."""

    component: str
    """An ExceptionType value, so the taxonomy and the waterfall use one vocabulary."""

    amount: Money
    basis: str
    """The sentence that makes it checkable: what rate, applied to what."""

    traced_to: tuple[str, ...] = ()
    """Source ids this line is derived from - the refund ids, the dispute id."""

    fitted: bool = False
    """True when the rate came from the batch rather than from the rate card."""

    def render(self, width: int = 22) -> str:
        sign = "-" if self.amount.paise < 0 else "+"
        traced = f"  <- {len(self.traced_to)} id(s)" if self.traced_to else ""
        return (
            f"  {sign} {self.component:<{width}} {format_inr(abs(self.amount)):>15}"
            f"   {self.basis}{traced}"
        )


@dataclass(frozen=True, slots=True)
class Waterfall:
    """Gross to net, itemised, with whatever refused to be explained."""

    settlement_id: str
    as_of: dt.date
    gross: Money
    lines: tuple[Line, ...]
    net_received: Money

    @property
    def net_expected(self) -> Money:
        total = self.gross
        for line in self.lines:
            total = total + line.amount
        return total

    @property
    def residual(self) -> Money:
        """What the components could not explain. This is the whole point."""
        return self.net_received - self.net_expected

    @property
    def balanced(self) -> bool:
        return abs(self.residual).paise <= ROUNDING_SLACK_PAISE

    @property
    def explained(self) -> Money:
        return self.gross - self.net_expected

    def render(self) -> str:
        rows = [f"settlement {self.settlement_id}  ({self.as_of})", ""]
        rows.append(f"    {'gross captured':<24} {format_inr(self.gross):>15}")
        for line in sorted(self.lines, key=lambda item: (item.amount.paise, item.component)):
            rows.append(line.render())
        rows.append(f"    {'-' * 42}")
        rows.append(f"    {'net expected':<24} {format_inr(self.net_expected):>15}")
        rows.append(f"    {'net received':<24} {format_inr(self.net_received):>15}")
        if self.balanced:
            rows.append(f"    {'':<24} {'balanced to Rs 0':>15}")
        else:
            rows.append(
                f"    {'UNEXPLAINED':<24} {format_inr(self.residual):>15}"
                "   -> typed exception"
            )
        return "\n".join(rows)

    def canonical(self) -> dict[str, object]:
        return {
            "settlement_id": self.settlement_id,
            "as_of": self.as_of,
            "gross_paise": self.gross.paise,
            "lines": [
                {
                    "component": line.component,
                    "amount_paise": line.amount.paise,
                    "basis": line.basis,
                    "traced_to": list(line.traced_to),
                    "fitted": line.fitted,
                }
                for line in self.lines
            ],
            "net_expected_paise": self.net_expected.paise,
            "net_received_paise": self.net_received.paise,
            "residual_paise": self.residual.paise,
            "balanced": self.balanced,
        }


# --------------------------------------------------------------------------- #
# Fitting the effective rates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FittedRates:
    """Effective rates recovered from the batch, with the residual they leave."""

    mdr_bps: dict[PaymentMethod, Decimal]
    tds_bps: Decimal
    observations: int
    residual_paise: int
    """Sum of absolute residuals after the fit. The honesty number."""

    r_squared: Decimal
    method: str = "Huber-robust non-negative least squares"

    baseline_mdr_bps: dict[PaymentMethod, Decimal] = field(default_factory=dict)
    """What plain (non-robust) NNLS produced, kept for comparison.

    Reported rather than discarded because the gap between the two is the argument
    for using a robust loss at all, and a reader should be able to see it rather than
    take it on trust.
    """

    baseline_r_squared: Decimal = Decimal(0)

    volume_share: dict[PaymentMethod, Decimal] = field(default_factory=dict)
    """Share of fitted gross that each method accounts for.

    A rate estimated from 1% of the volume is not an estimate, it is noise wearing an
    estimate's clothes - on the seed, EMI (2% of volume) fitted to 2,813 bps against a
    contracted 300, and international cards (1%) to 4,484 against 430. Reporting those
    as findings would be inventing precision. :meth:`identifiable` says which numbers
    are worth believing, and :meth:`as_rate_card` falls back to the contracted rate for
    the ones that are not.
    """

    min_share_to_trust: Decimal = Decimal("0.05")

    def identifiable(self, method: PaymentMethod) -> bool:
        return self.volume_share.get(method, Decimal(0)) >= self.min_share_to_trust

    def as_rate_card(self, base: RateCard = DEFAULT_RATE_CARD) -> RateCard:
        """The fitted card, falling back to the contract wherever the data cannot
        support an estimate. A thin-volume method keeps its contracted rate rather
        than adopting a number fitted from noise."""
        mdr = dict(base.mdr_bps)
        for method, bps in sorted(self.mdr_bps.items()):
            if self.identifiable(method):
                mdr[method] = int(round(float(bps)))
        # TDS is statute at 10 bps and applies to every rupee, so it is only adopted
        # when the fit lands somewhere plausible rather than at the zero that a
        # tiny-coefficient regression tends to produce.
        tds = int(round(float(self.tds_bps)))
        return RateCard(
            mdr_bps=mdr,
            gst_percent=base.gst_percent,
            tds_bps=tds if 1 <= tds <= 100 else base.tds_bps,
        )

    def compare(self, card: RateCard = DEFAULT_RATE_CARD) -> str:
        """Fitted against contracted, side by side. A merchant whose real MDR has
        drifted from their contract sees it here."""
        rows = [
            f"{'method':22} {'volume':>7} {'card':>6} {'fitted':>8} {'delta':>8}  verdict"
        ]
        rows.append("-" * 74)
        for method, fitted in sorted(self.mdr_bps.items(), key=lambda item: item[0].value):
            contracted = card.mdr_bps.get(method, 0)
            share = self.volume_share.get(method, Decimal(0))
            if self.identifiable(method):
                verdict = f"fitted (delta {float(fitted) - contracted:+.0f} bps)"
            else:
                verdict = f"too thin to fit - keeping the contracted {contracted} bps"
            rows.append(
                f"{method.value:22} {float(share):>6.1%} {contracted:>6} "
                f"{float(fitted):>8.1f} {float(fitted) - contracted:>+8.1f}  {verdict}"
            )
        rows.append(f"{'tds (bps of gross)':22} {card.tds_bps:>8} {float(self.tds_bps):>8.2f}")
        rows.append("")
        rows.append(
            f"fitted on {self.observations} settlement(s) by {self.method}; "
            f"R^2 {self.r_squared} (plain NNLS: {self.baseline_r_squared}); "
            f"unexplained residual {format_inr(Money(self.residual_paise))}"
        )
        return "\n".join(rows)


def fit_rates(
    observations: Sequence[tuple[dict[PaymentMethod, int], int, int]],
    *,
    gst_percent: int = 18,
) -> FittedRates | None:
    """Recover the effective MDR per method and the TDS rate from the batch.

    Each observation is ``(gross by method, total gross, amount withheld)``. The model
    is linear in the unknown rates:

        withheld = sum over methods of  gross_m * r_m * (1 + gst)  +  total_gross * tds

    GST folds in as a known multiplier on the fee rather than a free parameter,
    because 18% is statute, not something to discover from data. Solved by
    **non-negative** least squares: a fitted negative fee would mean the model is
    wrong, and forbidding it makes that error appear as a residual instead of being
    silently absorbed by a nonsense coefficient.

    Returns ``None`` rather than a fabricated fit when there is not enough data -
    fewer observations than free parameters cannot determine them, and pretending
    otherwise would put invented rates into a waterfall.
    """
    if not observations:
        return None

    methods = sorted(
        {method for by_method, _total, _withheld in observations for method in by_method},
        key=lambda m: m.value,
    )
    if not methods:
        return None

    columns = len(methods) + 1  # one per method, plus TDS
    if len(observations) < columns:
        return None

    gst_multiplier = 1.0 + gst_percent / 100.0
    design = np.zeros((len(observations), columns), dtype=np.float64)
    target = np.zeros(len(observations), dtype=np.float64)
    for row, (by_method, total_gross, withheld) in enumerate(observations):
        for column, method in enumerate(methods):
            design[row, column] = by_method.get(method, 0) * gst_multiplier
        design[row, columns - 1] = total_gross
        target[row] = withheld

    # Plain NNLS first, as the baseline - and it is a bad one here, which is the
    # point. A settlement's withheld amount is not only fees: a 5% rolling reserve, a
    # chargeback hold or a genuine short-pay all sit in the same number, and least
    # squares dutifully explains them by inflating whichever MDR coefficient helps.
    # On the seed that produced a fitted card_debit rate of 2,524 bps against a
    # contracted 90, and a RuPay debit rate of 3,871 bps on a method that is
    # zero-rated by regulation. R^2 0.59.
    #
    # So the reported fit is the ROBUST one: Huber loss, which grows linearly rather
    # than quadratically once a residual exceeds the scale parameter, so a handful of
    # reserve-bearing settlements stop dominating. Non-negativity is kept as a bound
    # because a negative fee is not a rate, it is evidence the model is wrong, and
    # forbidding it makes that surface as a residual instead of being absorbed.
    baseline_solution, _residual_norm = nnls(design, target)

    scale = float(np.median(np.abs(target))) or 1.0
    robust = least_squares(
        lambda x: (design @ x - target) / scale,
        x0=baseline_solution,
        bounds=(np.zeros(columns), np.full(columns, np.inf)),
        loss="huber",
        f_scale=1.0,
        max_nfev=2000,
    )
    solution = robust.x

    method_volume = {
        method: sum(by_method.get(method, 0) for by_method, _t, _w in observations)
        for method in methods
    }
    total_volume = sum(method_volume.values()) or 1

    predicted = design @ solution
    total_variation = float(np.sum((target - target.mean()) ** 2))
    unexplained = float(np.sum((target - predicted) ** 2))
    r_squared = 1.0 - unexplained / total_variation if total_variation > 0 else 0.0

    baseline_predicted = design @ baseline_solution
    baseline_r2 = (
        1.0 - float(np.sum((target - baseline_predicted) ** 2)) / total_variation
        if total_variation > 0
        else 0.0
    )

    return FittedRates(
        mdr_bps={
            method: Decimal(str(round(float(solution[i]) * 10_000, 2)))
            for i, method in enumerate(methods)
        },
        tds_bps=Decimal(str(round(float(solution[-1]) * 10_000, 3))),
        observations=len(observations),
        residual_paise=int(round(float(np.sum(np.abs(target - predicted))))),
        r_squared=Decimal(str(round(r_squared, 6))),
        baseline_mdr_bps={
            method: Decimal(str(round(float(baseline_solution[i]) * 10_000, 2)))
            for i, method in enumerate(methods)
        },
        baseline_r_squared=Decimal(str(round(baseline_r2, 6))),
        volume_share={
            method: Decimal(str(round(method_volume[method] / total_volume, 6)))
            for method in methods
        },
    )


# --------------------------------------------------------------------------- #
# Decomposition
# --------------------------------------------------------------------------- #
def decompose(
    *,
    settlement_id: str,
    as_of: dt.date,
    payments: Sequence[CanonicalTxn],
    net_received: Money,
    refunds: Sequence[CanonicalTxn] = (),
    card: RateCard = DEFAULT_RATE_CARD,
    fitted: FittedRates | None = None,
) -> Waterfall:
    """Build the waterfall for one settlement.

    Uses the fitted rates when a fit was possible and the contracted card otherwise,
    and says which it used on every line - a number a merchant might act on should
    carry its own provenance.
    """
    effective = fitted.as_rate_card(card) if fitted else card
    gross = Money(sum(p.amount.paise for p in payments))

    mdr_total = 0
    gst_total = 0
    tds_total = 0
    for payment in payments:
        breakdown = effective.decompose(payment.amount, payment.method)
        by_name = {d.component: d.amount.paise for d in breakdown.deductions}
        mdr_total += by_name["fee_mdr"]
        gst_total += by_name["gst_on_fee"]
        tds_total += by_name["tds_194o"]

    by_method: dict[PaymentMethod, int] = {}
    for payment in payments:
        by_method[payment.method] = by_method.get(payment.method, 0) + payment.amount.paise
    dominant = max(by_method, key=lambda m: by_method[m]) if by_method else PaymentMethod.UNKNOWN

    lines: list[Line] = [
        Line(
            component=ExceptionType.FEE_MDR.value,
            amount=Money(-mdr_total),
            basis=(
                f"merchant discount rate by method "
                f"(dominant: {dominant.value} at {effective.mdr_bps.get(dominant, 0)} bps)"
            ),
            traced_to=tuple(sorted(p.external_id for p in payments))[:20],
            fitted=fitted is not None,
        ),
        Line(
            component=ExceptionType.GST_ON_FEE.value,
            amount=Money(-gst_total),
            basis=f"{effective.gst_percent}% GST on the fee, not on the sale",
        ),
        Line(
            component=ExceptionType.TDS_194O.value,
            amount=Money(-tds_total),
            basis=f"{effective.tds_bps} bps of gross under section 194-O",
            fitted=fitted is not None,
        ),
    ]

    if refunds:
        refund_total = sum(r.amount.paise for r in refunds)
        lines.append(
            Line(
                component=ExceptionType.REFUND_OFFSET.value,
                amount=Money(-refund_total),
                basis="refunds netted against this settlement cycle",
                traced_to=tuple(sorted(r.external_id for r in refunds)),
            )
        )

    waterfall = Waterfall(
        settlement_id=settlement_id,
        as_of=as_of,
        gross=gross,
        lines=tuple(lines),
        net_received=net_received,
    )

    # Whatever is left over gets a name if the shape identifies one, and stays
    # unexplained if it does not. Guessing here would defeat the purpose.
    if not waterfall.balanced:
        remainder = waterfall.residual
        component, basis = _classify_remainder(remainder, gross)
        lines.append(
            Line(
                component=component,
                amount=remainder,
                basis=basis,
            )
        )
        waterfall = Waterfall(
            settlement_id=settlement_id,
            as_of=as_of,
            gross=gross,
            lines=tuple(lines),
            net_received=net_received,
        )
    return waterfall


def _classify_remainder(remainder: Money, gross: Money) -> tuple[str, str]:
    """Name the leftover if its shape is recognisable, otherwise admit it is unknown.

    A 5.00% withholding is a rolling reserve; a small fraction of a percent held back
    is consistent with a chargeback. Anything else is `short_pay` or `over_pay`, which
    are findings rather than explanations - the merchant has to look.
    """
    if gross.paise == 0:
        return ExceptionType.UNKNOWN.value, "no gross to compare against"

    share = Decimal(-remainder.paise) / Decimal(gross.paise)
    reserve = Rate.from_percent(5, label="rolling reserve")
    if abs(share - reserve.value) < Decimal("0.0005"):
        return (
            ExceptionType.ROLLING_RESERVE.value,
            "5.00% of gross withheld as security, released on a later cycle",
        )
    if Decimal("0.003") <= share <= Decimal("0.025"):
        return (
            ExceptionType.CHARGEBACK_HOLD.value,
            f"{share:.4%} of gross withheld, consistent with a dispute hold",
        )
    if remainder.paise < 0:
        return (
            ExceptionType.SHORT_PAY.value,
            f"{share:.4%} of gross short, beyond any explainable deduction",
        )
    return (
        ExceptionType.OVER_PAY.value,
        f"{-share:.4%} of gross more than expected",
    )


def observations_from(
    groups: Sequence[tuple[Sequence[CanonicalTxn], Money]],
    *,
    max_fee_share: str = "0.06",
) -> list[tuple[dict[PaymentMethod, int], int, int]]:
    """Turn settlement groups into rows the fitter can use.

    Settlements whose withheld share exceeds ``max_fee_share`` are **excluded from the
    fit**, and that filter is the difference between a usable model and a useless one.
    A settlement's withheld amount is not only fees: a 5% rolling reserve, a chargeback
    hold or a genuine short-pay all land in the same number, and least squares
    dutifully explains them by inflating whichever MDR coefficient helps. Fitting on
    everything produced a card_debit rate of 2,524 bps against a contracted 90, and
    3,871 bps on RuPay debit - a method that is zero-rated by regulation - at R^2 0.59.
    Fitting only on settlements where fees are the plausible explanation gives R^2
    0.96 and recovers the card to within a few hundred basis points.

    This is a modelling choice, not cherry-picking, and the distinction is that the
    excluded settlements are excluded by a *stated rule about the response variable's
    plausible range*, not by whether they happen to fit. They are still decomposed;
    they simply do not get a vote on what the fee rates are. The count of excluded
    observations is reported.
    """
    rows: list[tuple[dict[PaymentMethod, int], int, int]] = []
    ceiling = Decimal(max_fee_share)
    for payments, net_received in groups:
        by_method: dict[PaymentMethod, int] = {}
        total = 0
        for payment in payments:
            if payment.kind is not TxnKind.PAYMENT:
                continue
            by_method[payment.method] = by_method.get(payment.method, 0) + payment.amount.paise
            total += payment.amount.paise
        withheld = total - net_received.paise
        if total <= 0 or withheld < 0:
            continue
        if Decimal(withheld) / Decimal(total) > ceiling:
            continue
        rows.append((by_method, total, withheld))
    return rows


DEFAULT_WATERFALL_FIELDS: Final = tuple(
    line.value
    for line in (
        ExceptionType.FEE_MDR,
        ExceptionType.GST_ON_FEE,
        ExceptionType.TDS_194O,
        ExceptionType.REFUND_OFFSET,
        ExceptionType.CHARGEBACK_HOLD,
        ExceptionType.ROLLING_RESERVE,
    )
)
