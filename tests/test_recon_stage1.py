"""M6 gate: canonicalisation, the Indian normaliser, and exact Stage-1 matching.

The DoD is a baseline match rate with precision and recall recorded. The number that
matters here is not the match rate - Stage 1 alone is meant to be low-recall - but
**precision**, which should be 1.0. Stage 1's whole job is to make the claims that
cannot be wrong, so that the expensive stages inherit a smaller, harder problem. A
Stage 1 that trades precision for coverage has taken work away from the solver and
given it to a human.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.money import Money
from ingest.adapters.csv_bank import parse_statement_date
from ingest.adapters.fixtures import read_payments, read_refunds
from ingest.canonical import Direction, SourceKind, TxnKind
from recon.exceptions import ExceptionType
from recon.normalize import (
    ParseSource,
    Rail,
    counterparty_tokens,
    extract_utr,
    name_key,
    normalize_counterparty,
    normalize_reference,
    parse_narration,
    summarise,
)
from recon.pipeline import ReconConfig, canonicalise, reconcile


@pytest.fixture(scope="module")
def result():
    return reconcile()


# --------------------------------------------------------------------------- #
# The Indian normaliser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "spelling",
    [
        "Pillai Hardware P Ltd",
        "PILLAI HARDWARE PRIVATE LIMITED",
        "Pillai Hardware Pvt. Ltd",
        "Pillai Hardware Pvt Ltd",
        "Shri Pillai Hardware LLP",
        "M/S Pillai Hardware",
        "Pillai  Hardware",
        "Pillai Hardware",
    ],
)
def test_one_legal_entity_spelled_many_ways_collapses_to_one_key(spelling: str) -> None:
    """Four systems spell the same company four ways. No amount of edit distance
    handles `P Ltd` as reliably as knowing what it means."""
    assert normalize_counterparty(spelling) == "PILLAI HARDWARE"


def test_ampersand_and_and_are_the_same_company() -> None:
    assert normalize_counterparty("Sethi & Sons") == normalize_counterparty("SETHI AND SONS")


def test_stacked_legal_suffixes_are_all_stripped() -> None:
    """One pass is not enough for `Pvt Ltd Co`."""
    assert normalize_counterparty("Bose Exports Pvt Ltd Co") == "BOSE EXPORTS"


def test_normalisation_does_not_erase_the_actual_name() -> None:
    """A normaliser that maps everything to the empty string matches everything."""
    assert normalize_counterparty("Ltd") == ""
    assert normalize_counterparty("Nair Foods") == "NAIR FOODS"
    assert counterparty_tokens("Nair Foods Pvt Ltd") == {"NAIR", "FOODS"}


def test_phonetic_key_groups_transliteration_variants() -> None:
    """A blocking device, not a matching decision: two names sharing a key are worth
    comparing, nothing more."""
    assert name_key("Chatterjee Textiles") == name_key("Chatterjee Textiles Pvt Ltd")
    assert name_key("Mukherjee Foods") != name_key("Nair Foods")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NEFT-HDFC940235305794", "940235305794"),
        ("940235305794", "940235305794"),
        ("UPI/CR/274000517685", "274000517685"),
        ("  utr 940235305794 ", "940235305794"),
        ("IMPS/609112345678", "609112345678"),
        ("", ""),
    ],
)
def test_reference_normalisation_strips_rail_and_bank_prefixes(raw: str, expected: str) -> None:
    """`NEFT-HDFC940235305794` and `940235305794` are the same payment; if they do
    not compare equal, Stage 1 finds nothing."""
    assert normalize_reference(raw) == expected


def test_utr_extraction_takes_the_longest_digit_run() -> None:
    assert extract_utr("NEFT-ICIC232424838548-FOO 000490") == "232424838548"
    assert extract_utr("MISC CREDIT REF NA") == ""


# --------------------------------------------------------------------------- #
# Narration parsing - the AI-judgment measurement
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("narration", "rail", "utr"),
    [
        ("NEFT-UTIB940235305794-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT 000489", Rail.NEFT, "940235305794"),
        ("UPI/CR/274000517685/Sethi Hardware Private Limited/KKBK/@ibl", Rail.UPI, "274000517685"),
        ("IMPS/P2A/609112345678/RAZORPAY/SETTLEMENT", Rail.IMPS, "609112345678"),
        ("RTGS-HDFCR120260331001-ACME-REF", Rail.RTGS, "120260331001"),
    ],
)
def test_known_rail_formats_never_reach_a_model(narration: str, rail: Rail, utr: str) -> None:
    """A format is a regex, not a prompt. This is the whole AI-judgment argument."""
    parse = parse_narration(narration)
    assert parse.rail is rail
    assert parse.utr == utr
    assert parse.source is ParseSource.REGEX
    assert parse.complete and not parse.needs_llm()


def test_genuinely_ambiguous_text_is_routed_to_a_model() -> None:
    parse = parse_narration("MISC CREDIT 9060122 REF NA")
    assert parse.needs_llm()
    assert parse.rail is Rail.UNKNOWN


def test_an_empty_narration_is_not_a_model_call() -> None:
    parse = parse_narration("")
    assert parse.source is ParseSource.NONE


def test_extraction_records_character_spans_for_highlighting() -> None:
    """The UI shows a human exactly where a value came from; without spans the
    grounded why-string is just an assertion."""
    narration = "NEFT-UTIB940235305794-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT 000489"
    parse = parse_narration(narration)
    start, end = parse.spans["utr"]
    assert narration[start:end] == parse.utr
    start, end = parse.spans["counterparty"]
    assert narration[start:end] == parse.counterparty


def test_the_llm_call_rate_on_the_seed_is_low_and_measured(result) -> None:
    """"We called the LLM on 5% of rows" is a checkable claim, not an assertion."""
    bank_narrations = [
        row.raw_narration for row in result.rows.values() if row.source is SourceKind.BANK
    ]
    stats = summarise(bank_narrations)
    assert stats.total >= 15
    assert stats.llm_call_rate <= 0.15, stats.render()
    assert stats.deterministic_rate >= 0.85, stats.render()


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
def test_all_three_sources_load(result) -> None:
    by_source = {}
    for row in result.rows.values():
        by_source[row.source] = by_source.get(row.source, 0) + 1
    assert by_source[SourceKind.GATEWAY] > 200
    assert by_source[SourceKind.BANK] >= 15
    assert by_source[SourceKind.LEDGER] > 200


def test_refunds_are_signed_as_debits() -> None:
    """Signing at the boundary means the netting arithmetic downstream is a plain
    sum rather than a special case."""
    refunds = read_refunds()
    assert refunds.rows
    for row in refunds.rows:
        assert row.direction is Direction.DEBIT
        assert row.signed_amount.paise < 0
        assert row.kind is TxnKind.REFUND


def test_gateway_amounts_stay_integer_paise() -> None:
    payments = read_payments()
    for row in payments.rows[:20]:
        assert isinstance(row.amount.paise, int)
        assert row.amount.currency == "INR"


def test_a_corrupt_row_survives_as_a_typed_exception(result) -> None:
    """The single most dangerous thing an ingest layer can do is drop a row it
    cannot read: it loses money and then reports a clean close."""
    assert result.corrupt, "the seed contains deliberately corrupt rows"
    corrupt_exceptions = [
        e for e in result.exceptions if e.exception_type is ExceptionType.CORRUPT_ROW
    ]
    assert len(corrupt_exceptions) == len(result.corrupt)
    for exception in corrupt_exceptions:
        assert exception.reason
        assert exception.evidence.items, "a corrupt row must carry its raw payload"
        assert exception.suggested_action


def test_the_batch_continues_past_a_corrupt_row(result) -> None:
    bank_rows = [r for r in result.rows.values() if r.source is SourceKind.BANK]
    assert len(bank_rows) >= 15, "corrupt rows aborted the batch"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2026-03-04", (2026, 3, 4)), ("04/03/2026", (2026, 3, 4)), ("04-Mar-2026", (2026, 3, 4))],
)
def test_indian_date_ordering_is_preferred(text: str, expected: tuple[int, int, int]) -> None:
    """`03/04/2026` is the 3rd of April in India. Trying the American ordering first
    would mis-date a third of every statement and manufacture phantom timing breaks."""
    parsed = parse_statement_date(text)
    assert (parsed.year, parsed.month, parsed.day) == expected


def test_a_row_with_both_credit_and_debit_is_refused_not_guessed() -> None:
    from core.errors import MoneyError
    from ingest.adapters.csv_bank import _row_amount

    with pytest.raises(MoneyError):
        _row_amount({"credit": "100", "debit": "50"})
    with pytest.raises(MoneyError):
        _row_amount({"credit": "", "debit": ""})


# --------------------------------------------------------------------------- #
# Stage 0
# --------------------------------------------------------------------------- #
def test_canonicalisation_preserves_the_raw_alongside_the_normalised(result) -> None:
    """A normaliser bug must always be diagnosable: you can see both what arrived
    and what we made of it."""
    ledger = [r for r in result.rows.values() if r.source is SourceKind.LEDGER]
    assert ledger
    row = next(r for r in ledger if r.raw_counterparty)
    assert row.counterparty == normalize_counterparty(row.raw_counterparty)
    assert row.raw_counterparty != "" and row.raw != {}


def test_canonicalisation_is_idempotent(result) -> None:
    rows = list(result.rows.values())[:50]
    once, _ = canonicalise(rows)
    twice, _ = canonicalise(once)
    assert [r.counterparty for r in once] == [r.counterparty for r in twice]
    assert [r.reference for r in once] == [r.reference for r in twice]


# --------------------------------------------------------------------------- #
# Stage 1
# --------------------------------------------------------------------------- #
def test_stage1_makes_only_claims_that_cannot_be_wrong(result) -> None:
    """Every Stage 1 match carries confidence 1.0, a reason, and its evidence."""
    stage1 = [m for m in result.matches if m.stage.startswith("stage1")]
    assert stage1, "Stage 1 matched nothing"
    for match in stage1:
        assert match.confidence == Decimal(1)
        assert match.reason
        assert match.evidence.items
        assert match.evidence.stage == match.stage


def test_stage1_refuses_a_utr_match_whose_amounts_disagree(result) -> None:
    """A UTR that agrees on identity but not on amount is a *finding* - the short-pay
    case - and quietly matching it would hide the thing the merchant needs to see."""
    for match in result.matches:
        if match.stage != "stage1.utr":
            continue
        gateway = [result.rows[i] for i in match.gateway_ids]
        bank = [result.rows[i] for i in match.bank_ids]
        assert sum(r.amount.paise for r in gateway) == sum(r.amount.paise for r in bank)


def test_no_row_is_double_booked(result) -> None:
    """Invariant D.1.3."""
    assert result.check_no_double_spend() == []


def test_a_later_stage_may_only_add(result) -> None:
    """Invariant D.1.4: stages are monotone. Consumed-row counts never decrease."""
    running = 0
    for report in result.stages:
        assert report.rows_consumed >= 0
        running += report.rows_consumed
    assert running == result.matched_row_count


def test_every_stage_reports_its_contribution_and_wall_clock(result) -> None:
    assert [s.stage for s in result.stages] == ["stage0", "stage1", "stage2", "stage3"]
    for report in result.stages:
        assert report.elapsed_ms >= 0
        assert report.detail


def test_reconciliation_is_deterministic() -> None:
    a, b = reconcile(), reconcile()
    assert [m.match_id for m in a.matches] == [m.match_id for m in b.matches]
    assert [e.exception_id for e in a.exceptions] == [e.exception_id for e in b.exceptions]


# --------------------------------------------------------------------------- #
# The baseline the later stages must beat
# --------------------------------------------------------------------------- #
def test_stage1_precision_is_perfect_and_recall_is_honestly_low() -> None:
    """The M6 baseline, asserted so a later milestone cannot silently regress it.

    Stage 1 is *supposed* to be low-recall: it makes only the claims that cannot be
    wrong, leaving a smaller and harder problem for the solver. Precision below 1.0
    here would mean the exact keys are not exact.
    """
    from scripts.eval_pipeline import evaluate_pipeline

    report = evaluate_pipeline()
    by_name = {m.name: m for m in report.metrics}
    assert by_name["precision"].value == 1, "Stage 1's exact keys must never be wrong"
    assert by_name["recall"].value < Decimal("0.6"), (
        "the deterministic stages alone should not have high recall - if they do, "
        "either the metric is wrong or an exact key is matching things it should not"
    )
    # The denominator was 3,808 until M7, which was wrong: it took the cross product
    # of every payment and every invoice in a settlement group, claiming 196 pairs for
    # a 14-payment settlement when there are 14. Ground truth now records the explicit
    # 1:1 links, so the real figure is an order of magnitude smaller.
    assert 500 < by_name["true_pairs_total"].value < 1500
    assert by_name["llm_call_rate"].value <= Decimal("0.15")


def test_the_config_is_explicit_and_hashable() -> None:
    config = ReconConfig()
    assert config.settlement_cycle_days == 2
    assert config.amount_tolerance == Money.zero(), "Stage 1 is exact by design"
    assert config.canonical()["settlement_cycle_days"] == 2
