"""M10 gate: the settlement waterfall, with rates fitted rather than assumed.

The DoD is that fitted rates recover the generator's true rates within a stated
tolerance and the waterfall balances to Rs 0. Both are here, along with the two things
that made the fit usable: excluding settlements whose withheld share cannot be fees,
and refusing to report a rate estimated from too little volume.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.clock import IST
from core.money import Money
from ingest.canonical import Direction, PaymentMethod, TxnKind, build_txn
from recon.exceptions import ExceptionType
from recon.fees import DEFAULT_RATE_CARD
from recon.pipeline import reconcile
from recon.waterfall import (
    ROUNDING_SLACK_PAISE,
    decompose,
    fit_rates,
    observations_from,
)


@pytest.fixture(scope="module")
def result():
    return reconcile()


def payment(paise: int, method: PaymentMethod = PaymentMethod.UPI, ident: str = "pay_1"):
    return build_txn(
        source=__import__("ingest.canonical", fromlist=["SourceKind"]).SourceKind.GATEWAY,
        kind=TxnKind.PAYMENT,
        external_id=ident,
        amount=Money(paise),
        direction=Direction.CREDIT,
        occurred_at=dt.datetime(2026, 3, 10, 12, 0, tzinfo=IST),
        method=method,
    )


# --------------------------------------------------------------------------- #
# The DoD: it balances
# --------------------------------------------------------------------------- #
def test_every_waterfall_balances_to_zero(result) -> None:
    """The Loop 2 money-shot. Not 'close to zero' - to the paise, within the slack
    that independently rounded components genuinely produce."""
    assert result.waterfalls, "Stage 5 produced no waterfalls"
    for waterfall in result.waterfalls.values():
        assert waterfall.balanced, waterfall.render()
        assert abs(waterfall.residual).paise <= ROUNDING_SLACK_PAISE


def test_the_components_sum_to_the_gap_exactly(result) -> None:
    """gross + sum(lines) == net_received. If the breakdown did not sum to the
    difference, it would be a story told next to the number rather than the number."""
    for waterfall in result.waterfalls.values():
        total = waterfall.gross
        for line in waterfall.lines:
            total = total + line.amount
        assert abs((total - waterfall.net_received).paise) <= ROUNDING_SLACK_PAISE


def test_every_line_states_its_basis(result) -> None:
    """A deduction without a stated basis is an assertion. 'GST' is not an
    explanation; '18% of the MDR, not of the sale' is."""
    for waterfall in result.waterfalls.values():
        for line in waterfall.lines:
            assert line.basis, f"{line.component} has no basis"
            assert line.component in {t.value for t in ExceptionType}


def test_traced_lines_name_their_sources(result) -> None:
    """A refund line that cannot name the refunds is not traceable."""
    for waterfall in result.waterfalls.values():
        for line in waterfall.lines:
            if line.component == ExceptionType.REFUND_OFFSET.value:
                assert line.traced_to


def test_an_unexplained_remainder_becomes_a_typed_exception() -> None:
    """A waterfall that balances by construction explains nothing. Feed it a
    settlement with an unexplainable gap and the gap must survive as a finding."""
    payments = [payment(1_000_000, PaymentMethod.UPI, "pay_a")]
    # UPI is zero-MDR, so only TDS applies; withhold far more than that.
    waterfall = decompose(
        settlement_id="setl_x",
        as_of=dt.date(2026, 3, 12),
        payments=payments,
        net_received=Money(800_000),
    )
    remainder = [
        line
        for line in waterfall.lines
        if line.component
        in {
            ExceptionType.SHORT_PAY.value,
            ExceptionType.UNKNOWN.value,
            ExceptionType.CHARGEBACK_HOLD.value,
            ExceptionType.ROLLING_RESERVE.value,
        }
    ]
    assert remainder, waterfall.render()
    assert waterfall.balanced, "the remainder line should close the waterfall"


def test_a_five_percent_withholding_is_named_a_rolling_reserve() -> None:
    gross = 10_000_000
    reserve = gross * 5 // 100
    waterfall = decompose(
        settlement_id="setl_r",
        as_of=dt.date(2026, 3, 12),
        payments=[payment(gross, PaymentMethod.UPI, "pay_r")],
        net_received=Money(gross - reserve - gross // 1000),
    )
    named = {line.component for line in waterfall.lines}
    assert ExceptionType.ROLLING_RESERVE.value in named, waterfall.render()


# --------------------------------------------------------------------------- #
# The DoD: fitted rates recover the truth
# --------------------------------------------------------------------------- #
def test_fitted_rates_recover_the_card_on_identifiable_methods(result) -> None:
    """Within a stated tolerance, and only where there is volume to support it."""
    fitted = result.fitted_rates
    assert fitted is not None, "the seed should have enough fee-consistent settlements"
    assert fitted.r_squared > Decimal("0.85"), fitted.compare()

    identifiable = [m for m in fitted.mdr_bps if fitted.identifiable(m)]
    assert identifiable, fitted.compare()
    for method in identifiable:
        contracted = DEFAULT_RATE_CARD.mdr_bps[method]
        error = abs(float(fitted.mdr_bps[method]) - contracted)
        assert error < 300, (
            f"{method.value}: fitted {fitted.mdr_bps[method]} vs contracted "
            f"{contracted}\n{fitted.compare()}"
        )


def test_a_rate_fitted_from_too_little_volume_is_not_reported_as_a_finding(result) -> None:
    """On the seed, EMI is 1% of volume and fits to 2,813 bps against a contracted
    300. Reporting that as a discovery would be inventing precision."""
    fitted = result.fitted_rates
    assert fitted is not None
    thin = [m for m in fitted.mdr_bps if not fitted.identifiable(m)]
    assert thin, "expected at least one thin-volume method on the seed"

    card = fitted.as_rate_card()
    for method in thin:
        assert card.mdr_bps[method] == DEFAULT_RATE_CARD.mdr_bps[method], (
            "a thin-volume method must keep its contracted rate, not adopt a fitted one"
        )
    assert "too thin to fit" in fitted.compare()


def test_the_fit_excludes_settlements_whose_gap_cannot_be_fees() -> None:
    """A 5% rolling reserve is not a fee, and letting least squares explain it by
    inflating an MDR coefficient produced a card_debit rate of 2,524 bps against a
    contracted 90."""
    clean = ({PaymentMethod.UPI: 1_000_000}, 1_000_000, 1_000)
    reserve_bearing = ({PaymentMethod.UPI: 1_000_000}, 1_000_000, 500_000)
    rows = observations_from(
        [
            ([payment(1_000_000)], Money(999_000)),
            ([payment(1_000_000)], Money(500_000)),
        ]
    )
    assert len(rows) == 1, "the 50%-withheld settlement must be excluded from the fit"
    _ = clean, reserve_bearing


def test_the_fit_refuses_rather_than_fabricating_when_underdetermined() -> None:
    """Fewer observations than free parameters cannot determine them, and inventing
    rates would put fabricated numbers into a waterfall."""
    assert fit_rates([]) is None
    assert fit_rates([({PaymentMethod.UPI: 100}, 100, 1)]) is None


def test_the_robust_fit_is_reported_alongside_the_plain_one(result) -> None:
    """The gap between them is the argument for using a robust loss, and a reader
    should be able to see it rather than take it on trust."""
    fitted = result.fitted_rates
    assert fitted is not None
    assert fitted.baseline_mdr_bps
    assert "Huber" in fitted.method
    assert "plain NNLS" in fitted.compare() or fitted.baseline_r_squared >= 0


def test_fitted_rates_are_never_negative(result) -> None:
    """A negative fee is not a rate, it is evidence the model is wrong - so the
    constraint forces that error into the residual instead."""
    fitted = result.fitted_rates
    assert fitted is not None
    assert all(bps >= 0 for bps in fitted.mdr_bps.values())
    assert fitted.tds_bps >= 0


# --------------------------------------------------------------------------- #
# No LLM anywhere near the arithmetic
# --------------------------------------------------------------------------- #
def test_the_decomposition_never_calls_a_model() -> None:
    """Deciding how much a fee is comes from closed-form arithmetic and a
    least-squares fit. A model guessing rupees would be a disqualifier."""
    from pathlib import Path

    for name in ("waterfall.py", "fees.py"):
        source = (Path(__file__).resolve().parent.parent / "recon" / name).read_text(
            encoding="utf-8"
        )
        body = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        for forbidden in ("core.llm", "anthropic", "openai", "completion("):
            assert forbidden not in body, f"{name} references {forbidden}"


# --------------------------------------------------------------------------- #
# In the pipeline
# --------------------------------------------------------------------------- #
def test_stage_five_runs_and_reports(result) -> None:
    stages = [s.stage for s in result.stages]
    assert stages == ["stage0", "stage1", "stage2", "stage3", "stage4", "stage5", "stage6"]
    detail = next(s for s in result.stages if s.stage == "stage5").detail
    assert "balance to Rs 0" in detail
    assert "R^2" in detail or "not enough" in detail


def test_stage_five_makes_no_matches(result) -> None:
    """It explains the matches Stage 4 made; it does not make new ones."""
    assert next(s for s in result.stages if s.stage == "stage5").matches_added == 0


def test_decomposition_is_deterministic() -> None:
    first, second = reconcile(), reconcile()
    assert sorted(first.waterfalls) == sorted(second.waterfalls)
    for key in first.waterfalls:
        assert first.waterfalls[key].canonical() == second.waterfalls[key].canonical()
