"""M8 gate: Fellegi-Sunter linkage with EM-fitted, explainable weights.

The DoD is a match-rate improvement over M6 with P/R reported, and a per-field weight
breakdown rendered for one example. The tests below also pin the two modelling
mistakes that measuring - rather than assuming - turned up:

* fitting EM on the *residue* left after Stage 1 gave a biased sample, and the model
  learned that landing in the expected settlement window was evidence *against* a
  match. EM is now fitted on the full candidate set;
* comparing dates through a T+2 projection for every relation inverted the date
  weights, because a payment and its invoice are both dated on the day of the sale.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest

from core.clock import IST
from core.money import Money
from ingest.canonical import Direction, PaymentMethod, SourceKind, TxnKind, build_txn
from recon.fellegi_sunter import (
    FIELDS,
    SMOOTHING,
    WEIGHT_CLAMP,
    FellegiSunterModel,
    Level,
    compare,
    fit_em,
    score_candidates,
)
from recon.pipeline import ReconConfig, reconcile


@pytest.fixture(scope="module")
def result():
    return reconcile()


@pytest.fixture(scope="module")
def fitted(result):
    return score_candidates(result.rows, sorted(result.candidates.pairs))


def row(
    *,
    source: SourceKind,
    kind: TxnKind = TxnKind.PAYMENT,
    external_id: str = "x",
    paise: int = 100_000,
    day: dt.date = dt.date(2026, 3, 10),
    reference: str = "",
    counterparty: str = "",
    narration: str = "",
    settled_on: dt.date | None = None,
):
    return build_txn(
        source=source,
        kind=kind,
        external_id=external_id,
        amount=Money(paise),
        direction=Direction.CREDIT,
        occurred_at=dt.datetime.combine(day, dt.time(12, 0), tzinfo=IST),
        settled_on=settled_on,
        reference=reference,
        counterparty=counterparty,
        raw_narration=narration,
        method=PaymentMethod.UPI,
    )


# --------------------------------------------------------------------------- #
# Comparison levels
# --------------------------------------------------------------------------- #
def test_identical_references_are_exact() -> None:
    left = row(source=SourceKind.GATEWAY, reference="940235305794")
    right = row(source=SourceKind.BANK, reference="940235305794")
    assert compare(left, right)[0] is Level.EXACT


def test_a_missing_field_carries_no_evidence() -> None:
    """A field neither side has must not become evidence. Letting EM weight MISSING
    would let absence of data masquerade as data."""
    left = row(source=SourceKind.GATEWAY, reference="")
    right = row(source=SourceKind.BANK, reference="")
    assert compare(left, right)[0] is Level.MISSING

    model = fit_em([compare(left, right)])
    assert model.weight(0, Level.MISSING) == 0.0


def test_amount_levels_track_the_fee_structure() -> None:
    """3% covers MDR + GST + TDS; 25% covers a netted settlement."""
    base = row(source=SourceKind.GATEWAY, paise=1_000_000)
    assert compare(base, row(source=SourceKind.BANK, paise=1_000_000))[1] is Level.EXACT
    assert compare(base, row(source=SourceKind.BANK, paise=980_000))[1] is Level.STRONG
    assert compare(base, row(source=SourceKind.BANK, paise=800_000))[1] is Level.WEAK
    assert compare(base, row(source=SourceKind.BANK, paise=100_000))[1] is Level.DISAGREE


def test_dates_are_compared_through_the_relation_not_a_blanket_projection() -> None:
    """A payment and its invoice are both dated on the day of the sale. Projecting
    T+2 here made every true pair look two days off, and EM dutifully learned that a
    two-day gap indicated a match - the weights came out inverted."""
    payment = row(source=SourceKind.GATEWAY, day=dt.date(2026, 3, 10))
    invoice = row(source=SourceKind.LEDGER, kind=TxnKind.INVOICE, day=dt.date(2026, 3, 10))
    assert compare(payment, invoice)[2] is Level.EXACT

    # But a payment to a bank credit genuinely is T+2 across the bank calendar.
    credit = row(
        source=SourceKind.BANK,
        kind=TxnKind.SETTLEMENT,
        day=dt.date(2026, 3, 12),
        settled_on=dt.date(2026, 3, 12),
    )
    assert compare(payment, credit)[2] is Level.EXACT


def test_a_long_weekend_does_not_read_as_a_date_disagreement() -> None:
    """Thu 12 Mar 2026 settles Mon 16 Mar: Sat 14 is a 2nd Saturday, Sun 15 closed."""
    payment = row(source=SourceKind.GATEWAY, day=dt.date(2026, 3, 12))
    credit = row(
        source=SourceKind.BANK,
        kind=TxnKind.SETTLEMENT,
        day=dt.date(2026, 3, 16),
        settled_on=dt.date(2026, 3, 16),
    )
    assert compare(payment, credit)[2] is Level.EXACT


def test_counterparty_levels_use_the_indian_normaliser() -> None:
    left = row(source=SourceKind.LEDGER, counterparty="PILLAI HARDWARE")
    same = row(source=SourceKind.BANK, counterparty="PILLAI HARDWARE")
    other = row(source=SourceKind.BANK, counterparty="NAIR FOODS")
    assert compare(left, same)[3] is Level.EXACT
    assert compare(left, other)[3] is Level.DISAGREE


# --------------------------------------------------------------------------- #
# EM
# --------------------------------------------------------------------------- #
def test_em_converges_on_the_real_candidate_set(fitted) -> None:
    model, _scores = fitted
    assert model.converged, f"EM did not converge in {model.iterations} iterations"
    assert 0 < model.lambda_ < 1


def test_em_is_deterministic(result) -> None:
    first, _ = score_candidates(result.rows, sorted(result.candidates.pairs))
    second, _ = score_candidates(result.rows, sorted(result.candidates.pairs))
    assert first.m == second.m and first.u == second.u
    assert first.lambda_ == second.lambda_


def test_em_learns_that_a_shared_reference_is_strong_evidence(fitted) -> None:
    """Not because we told it to. u(reference exact) is tiny because random pairs
    almost never share a UTR, and EM discovers that from the data."""
    model, _scores = fitted
    reference_index = next(i for i, f in enumerate(FIELDS) if f.name == "reference")
    assert model.weight(reference_index, Level.EXACT) > 5
    assert model.weight(reference_index, Level.DISAGREE) < 0


def test_em_learns_the_date_weights_in_the_right_direction(fitted) -> None:
    """The regression test for the inverted-weights bug. Landing on the expected
    settlement date must be positive evidence."""
    model, _scores = fitted
    date_index = next(i for i, f in enumerate(FIELDS) if f.name == "date")
    assert model.weight(date_index, Level.EXACT) > 0, model.weight_table()


def test_weights_are_clamped_so_no_single_field_decides(fitted) -> None:
    """A field is evidence, not a verdict."""
    model, _scores = fitted
    for i in range(len(FIELDS)):
        for level in (Level.EXACT, Level.STRONG, Level.WEAK, Level.DISAGREE):
            assert abs(model.weight(i, level)) <= WEIGHT_CLAMP + 1e-9


def test_smoothing_prevents_an_infinite_veto() -> None:
    """Without it, a level that never co-occurs with a match drives m to zero and one
    unlucky field silently vetoes every pair."""
    assert SMOOTHING > 0
    vectors = [(Level.EXACT,) * len(FIELDS)] * 20
    model = fit_em(vectors)
    for i in range(len(FIELDS)):
        for level in (Level.EXACT, Level.DISAGREE):
            assert math.isfinite(model.weight(i, level))


def test_em_on_an_empty_candidate_set_is_uniform_not_a_crash() -> None:
    model = fit_em([])
    assert all(abs(sum(row_) - 1.0) < 1e-6 for row_ in model.m)


def test_probability_never_overflows_and_stays_in_range() -> None:
    """`2**odds` overflows long before the probability stops being ~1, so the
    conversion is guarded at +/-60 bits.

    Note the bound is on *overflow*, not on reaching exactly 0 or 1: five clamped
    fields sum to -60 bits, which lands on the guard boundary and yields a tiny but
    finite 8.7e-19. That is correct and better than snapping to zero - a probability
    of exactly 0 would claim certainty the model does not have, and this value flows
    into the policy engine as a confidence.
    """
    model = FellegiSunterModel(
        m=[[0.0, 0.0, 0.0, 1.0, 0.0]] * len(FIELDS),
        u=[[1.0, 0.0, 0.0, 0.0, 0.0]] * len(FIELDS),
        lambda_=0.5,
    )
    high = model.probability([Level.EXACT] * len(FIELDS))
    low = model.probability([Level.DISAGREE] * len(FIELDS))
    assert high == 1.0
    assert 0.0 <= low < 1e-12
    assert math.isfinite(low)

    # Far past the guard in both directions, it must still return a real number.
    wide = FellegiSunterModel(
        m=[[0.0] * 4 + [0.0] for _ in FIELDS],
        u=[[0.0] * 4 + [0.0] for _ in FIELDS],
        lambda_=0.5,
    )
    assert 0.0 <= wide.probability([Level.EXACT] * len(FIELDS)) <= 1.0


# --------------------------------------------------------------------------- #
# Explainability - the DoD's rendered breakdown
# --------------------------------------------------------------------------- #
def test_every_score_carries_a_per_field_breakdown(fitted) -> None:
    """A total of 14.2 bits tells a human nothing; the breakdown tells them
    everything, and it is what the triage UI renders as a diverging bar chart."""
    _model, scores = fitted
    assert scores
    for score in scores[:50]:
        assert len(score.weights) == len(FIELDS)
        for weight in score.weights:
            assert weight.description, "a field weight with no reason is not evidence"
            assert weight.field in {f.name for f in FIELDS}


def test_the_weight_breakdown_renders_as_readable_text(fitted) -> None:
    _model, scores = fitted
    best = max(scores, key=lambda s: s.total)
    rendered = best.render()
    assert "match weight" in rendered
    assert "bits" in rendered
    assert len(rendered.splitlines()) == len(FIELDS) + 1


def test_the_fitted_weight_table_is_small_enough_to_read(fitted) -> None:
    """The single most useful artefact for convincing a finance team the model is not
    a black box."""
    model, _scores = fitted
    table = model.weight_table()
    assert "lambda" in table
    assert "converged" in table
    assert len(table.splitlines()) < 40


def test_the_total_is_exactly_the_sum_of_its_parts(fitted) -> None:
    """If the breakdown did not sum to the score, the explanation would be a story
    told next to the decision rather than the decision itself."""
    _model, scores = fitted
    for score in scores[:100]:
        assert abs(sum(w.weight for w in score.weights) - score.total) < 1e-9


# --------------------------------------------------------------------------- #
# Stage 3 in the pipeline
# --------------------------------------------------------------------------- #
def test_stage_three_runs_and_reports(result) -> None:
    assert [s.stage for s in result.stages] == ["stage0", "stage1", "stage2", "stage3", "stage4"]
    stage3 = next(s for s in result.stages if s.stage == "stage3")
    assert "clerical-review band" in stage3.detail


def test_stage_three_never_double_books(result) -> None:
    assert result.check_no_double_spend() == []


def test_stage_three_links_are_mutual_best_and_one_to_one(result) -> None:
    for match in result.matches:
        if not match.stage.startswith("stage3"):
            continue
        assert len(match.all_ids) == 2, "Stage 3 is 1:1; many-to-one belongs to Stage 4"


def test_stage_three_matches_carry_confidence_and_evidence(result) -> None:
    for match in result.matches:
        if not match.stage.startswith("stage3"):
            continue
        assert Decimal(0) <= match.confidence <= Decimal(1)
        assert len(match.evidence.items) == len(FIELDS)
        assert "bits" in match.reason


def test_the_clerical_review_band_becomes_typed_exceptions() -> None:
    """F-S's middle region is a human's decision, not a coin flip. Widening the band
    must move pairs into exceptions rather than into matches."""
    wide = reconcile(config=ReconConfig(link_upper_bits=45.0, link_lower_bits=-60.0))
    banded = [
        e for e in wide.exceptions if "clerical-review band" in (e.evidence.abstained_because or "")
    ]
    assert banded, "no pairs landed in the clerical-review band even at a 45-bit threshold"
    for exception in banded:
        assert exception.evidence.items
        assert exception.suggested_action


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #
def test_duplicates_are_detected_and_reported(result) -> None:
    """The five confidently-wrong matches at M8 were all duplicate payments being
    linked to the invoices their originals should have owned. No threshold could
    exclude them - the evidence was genuinely identical, because the rows were."""
    from recon.exceptions import ExceptionType

    duplicates = [e for e in result.exceptions if e.exception_type is ExceptionType.DUPLICATE]
    assert duplicates, "the seed contains injected duplicates that must be caught"
    for exception in duplicates:
        assert len(exception.source_ids) == 2
        assert "identical" in exception.reason
        assert exception.evidence.stage == "stage1.dedupe"


def test_a_duplicate_is_set_aside_never_discarded(result) -> None:
    """Reported, not deleted: an operator has to be able to see what was held back."""
    assert result.suppressed_ids
    for row_id in result.suppressed_ids:
        assert row_id in result.rows, "a suppressed row must still be inspectable"
    assert not (result.suppressed_ids & result.consumed_ids())


def test_deduplication_is_strict_and_deterministic() -> None:
    """Only byte-identical rows. A near-duplicate is a finding for a human, not
    something to silently drop - and the survivor cannot depend on ingest order."""
    first, second = reconcile(), reconcile()
    assert first.suppressed_ids == second.suppressed_ids


# --------------------------------------------------------------------------- #
# The DoD
# --------------------------------------------------------------------------- #
def test_match_rate_and_recall_improve_over_the_m6_baseline() -> None:
    from scripts.eval_pipeline import evaluate_pipeline

    report = evaluate_pipeline()
    by_name = {m.name: m for m in report.metrics}
    # M6 (deterministic only, corrected denominator): match rate 0.9084, recall 0.3324
    assert by_name["match_rate"].value > Decimal("0.9084")
    assert by_name["recall"].value > Decimal("0.3324")
    assert by_name["auto_post_precision"].value == 1, (
        "precision must not be traded for coverage on anything posted unsupervised; "
        "duplicates were the reason it once fell"
    )
