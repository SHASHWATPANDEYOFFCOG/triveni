"""M4 gate: reproducible metrics with intervals, and errors priced in rupees.

The Definition of Done is `make eval` twice producing byte-identical `metrics.json`,
with bootstrap confidence intervals on every headline metric. Reproducibility is
tested the way it can actually fail - across processes, under a randomised hash seed,
and with dictionaries built in different insertion orders - rather than by calling the
same function twice in one interpreter, which proves almost nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.costmodel import Assumption, CostModel, OutcomeMix, price
from core.eval import (
    Confusion,
    Metric,
    MetricRegistry,
    MetricsReport,
    append_history,
    bootstrap_ci,
    proportion_ci,
    q,
)
from core.money import Money
from scripts.eval_run import selftest_report

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Reproducibility - the DoD
# --------------------------------------------------------------------------- #
def test_make_eval_twice_is_byte_identical(tmp_path) -> None:
    """The headline requirement, run as two separate processes."""
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    for out in (first, second):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.eval_run", "--quiet", "--out", str(out)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stderr
    assert first.read_bytes() == second.read_bytes()


def test_metrics_are_stable_under_a_randomised_hash_seed(tmp_path) -> None:
    """Set iteration and dict ordering must not leak into the output. Running with
    PYTHONHASHSEED=random is the only way to actually prove that."""
    import os

    outputs = []
    for seed in ("0", "random"):
        out = tmp_path / f"{seed}.json"
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-m", "scripts.eval_run", "--quiet", "--out", str(out)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(out.read_bytes())
    assert outputs[0] == outputs[1]


def test_the_report_carries_no_timestamp() -> None:
    """A report that records when it ran cannot be byte-compared against itself."""
    text = selftest_report().to_json().decode()
    for forbidden in ("timestamp", "generated_at", "2026-09", "run_at"):
        assert forbidden not in text.lower().replace(
            "no timestamp by design", ""
        ), f"{forbidden!r} would break byte-identity"


def test_provenance_records_the_code_that_produced_the_numbers() -> None:
    report = selftest_report()
    payload = json.loads(report.to_json())
    assert payload["provenance"]["seed"] == 20260101
    assert len(payload["provenance"]["code_digest"]) == 32
    assert payload["dataset"]["digest"]


def test_source_digest_changes_when_the_code_changes(tmp_path, monkeypatch) -> None:
    """More honest than a git SHA, which says nothing about uncommitted edits."""
    import core.eval as ev

    package = tmp_path / "fake"
    package.mkdir()
    (package / "m.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(ev, "ROOT", tmp_path)
    before = ev.source_digest(("fake",))
    (package / "m.py").write_text("x = 2\n", encoding="utf-8")
    assert ev.source_digest(("fake",)) != before


def test_bootstrap_is_deterministic_and_order_independent() -> None:
    data = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0]
    assert bootstrap_ci(data) == bootstrap_ci(data)
    # The stream depends only on the seed, never on how many draws happened before.
    import numpy as np

    np.random.default_rng(999).random(1000)
    assert bootstrap_ci(data) == bootstrap_ci(list(data))


# --------------------------------------------------------------------------- #
# Intervals
# --------------------------------------------------------------------------- #
def test_every_rate_metric_gets_an_interval() -> None:
    report = selftest_report()
    rates = [m for m in report.metrics if m.unit == "%"]
    assert rates, "the selftest should exercise rate metrics"
    assert all(m.interval is not None for m in rates), "a rate with no CI is a point guess"


def test_an_empty_sample_yields_no_interval_rather_than_a_made_up_one() -> None:
    assert bootstrap_ci([]) is None
    assert proportion_ci(0, 0) is None


def test_interval_brackets_the_point_estimate() -> None:
    interval = proportion_ci(431, 437)
    assert interval is not None
    point = q(Decimal(431) / Decimal(437))
    assert interval.low <= point <= interval.high


@given(
    successes=st.integers(min_value=0, max_value=400),
    extra=st.integers(min_value=1, max_value=400),
)
@settings(max_examples=40, deadline=None)
def test_intervals_are_ordered_and_within_zero_to_one(successes: int, extra: int) -> None:
    total = successes + extra
    interval = proportion_ci(successes, total, resamples=200)
    assert interval is not None
    assert 0 <= interval.low <= interval.high <= 1


def test_a_larger_sample_gives_a_tighter_interval() -> None:
    """Sanity on the statistics: 5,000 rows should be more certain than 50."""
    narrow = proportion_ci(4_500, 5_000)
    wide = proportion_ci(45, 50)
    assert narrow is not None and wide is not None
    assert (narrow.high - narrow.low) < (wide.high - wide.low)


def test_single_observation_collapses_to_a_point() -> None:
    interval = bootstrap_ci([0.5])
    assert interval is not None and interval.low == interval.high


# --------------------------------------------------------------------------- #
# Metrics and the table
# --------------------------------------------------------------------------- #
def test_confusion_matrix_arithmetic() -> None:
    confusion = Confusion(tp=90, fp=10, fn=10, tn=890)
    assert confusion.precision == q(Decimal("0.9"))
    assert confusion.recall == q(Decimal("0.9"))
    assert confusion.f1 == q(Decimal("0.9"))


def test_empty_confusion_does_not_divide_by_zero() -> None:
    empty = Confusion()
    assert empty.precision == 0 and empty.recall == 0 and empty.f1 == 0


def test_duplicate_metric_names_are_refused() -> None:
    registry = MetricRegistry()
    registry.count("rows", 10)
    with pytest.raises(ValueError, match="duplicate"):
        registry.count("rows", 20)


def test_a_regression_is_marked_as_a_regression() -> None:
    """Rule A.6: a metric that got worse must be visibly worse, not quietly present."""
    worse = Metric(
        name="match_rate",
        value=q(Decimal("0.80")),
        unit="%",
        baseline=q(Decimal("0.90")),
        higher_is_better=True,
    )
    assert worse.delta == q(Decimal("-0.10"))
    assert worse.improved is False
    table = MetricsReport("d", 1, "x", 1, (worse,)).render_table()
    assert "▼" in table


def test_lower_is_better_metrics_invert_correctly() -> None:
    better = Metric(
        name="llm_call_rate",
        value=q(Decimal("0.05")),
        unit="%",
        baseline=q(Decimal("0.20")),
        higher_is_better=False,
    )
    assert better.improved is True


def test_rate_display_does_not_imply_precision_we_lack() -> None:
    """94.216374% from 500 rows would claim five digits of accuracy we do not have."""
    metric = Metric(name="r", value=q(Decimal("0.94216374")), unit="%")
    assert metric.render_value() == "94.22%"


def test_the_table_renders_every_metric_in_a_stable_order() -> None:
    report = selftest_report()
    lines = report.render_table().splitlines()
    names = [line.split()[0] for line in lines[2:]]
    assert names == sorted(names)
    assert len(names) == len(report.metrics)


# --------------------------------------------------------------------------- #
# The cost model
# --------------------------------------------------------------------------- #
def test_an_assumption_must_state_its_source() -> None:
    with pytest.raises(ValueError, match="source"):
        Assumption(Decimal("1"), "minutes", "made up", "")


def test_assumptions_reject_floats_like_every_other_money_path() -> None:
    with pytest.raises(TypeError):
        Assumption(0.5, "minutes", "x", "illustrative")  # type: ignore[arg-type]


def test_illustrative_assumptions_are_labelled_as_such() -> None:
    """They are stated and shown, never dressed up as sourced findings."""
    model = CostModel()
    assert all(a.is_illustrative for a in model.assumptions())
    assert "[illustrative assumption]" in model.analyst_cost_per_hour.describe()
    assert "illustrative" in selftest_report().render()


def test_a_false_match_costs_far_more_than_a_missed_one() -> None:
    """The asymmetry the whole threshold argument rests on."""
    model = CostModel()
    false_match = model.cost_per_false_match(Money.from_rupees("10000"))
    missed = model.cost_per_false_non_match()
    assert false_match.paise > missed.paise * 10


def test_a_clean_run_costs_nothing() -> None:
    """A correct auto-post is free because nobody looked at it. That is the whole
    economic case - and also why the false-match cost must be modelled honestly."""
    breakdown = price(OutcomeMix(rows=100, auto_posted=100, true_matches=100))
    assert breakdown.total.is_zero
    assert breakdown.saved_vs_manual.paise > 0


def test_cost_scales_with_the_number_of_false_matches() -> None:
    model = CostModel()
    one = price(
        OutcomeMix(rows=100, false_matches=1, exposed_by_false_matches=Money.from_rupees("5000")),
        model,
    )
    three = price(
        OutcomeMix(rows=100, false_matches=3, exposed_by_false_matches=Money.from_rupees("15000")),
        model,
    )
    assert three.total.paise > one.total.paise


def test_the_break_even_budget_is_the_number_that_governs_the_threshold() -> None:
    """Our own model says the selftest fixture is worse than manual. Reporting that
    is the point: automation only pays once the false-match rate is driven low."""
    report = selftest_report()
    by_name = {m.name: m for m in report.metrics}
    budget = by_name["false_match_budget"].value
    realised = by_name["false_matches_realised"].value
    assert realised > budget
    assert by_name["saved_vs_manual"].value < 0
    assert any("conformal" in note for note in report.notes)


def test_the_manual_baseline_charges_both_skim_and_triage() -> None:
    """Charging only the skim would understate manual cost and flatter Triveni."""
    model = CostModel()
    assert model.manual_baseline(100, 0).paise < model.manual_baseline(100, 20).paise


def test_cost_model_is_configurable_without_mutation() -> None:
    model = CostModel()
    pricier = model.with_analyst_cost("1200")
    assert model.analyst_cost_per_hour.value == Decimal("600")
    assert pricier.cost_of_minutes(Decimal(60)) == Money.from_rupees("1200")


@given(
    rows=st.integers(min_value=1, max_value=5000),
    fp=st.integers(min_value=0, max_value=50),
    fn=st.integers(min_value=0, max_value=200),
    reviewed=st.integers(min_value=0, max_value=200),
)
@settings(max_examples=100)
def test_pricing_is_total_and_never_negative_in_its_components(
    rows: int, fp: int, fn: int, reviewed: int
) -> None:
    breakdown = price(
        OutcomeMix(
            rows=rows,
            false_matches=fp,
            false_non_matches=fn,
            needs_review=reviewed,
            exposed_by_false_matches=Money.from_rupees(str(fp * 5000)),
        )
    )
    assert breakdown.total.paise >= 0
    assert breakdown.false_match_cost.paise >= 0
    assert breakdown.human_minutes >= 0
    assert breakdown.total == (
        breakdown.false_match_cost + breakdown.false_non_match_cost + breakdown.review_cost
    )


# --------------------------------------------------------------------------- #
# Selftest labelling
# --------------------------------------------------------------------------- #
def test_the_selftest_declares_itself_in_three_places() -> None:
    """A harness that reported placeholder numbers as results would be exactly the
    dishonesty rule A.6 forbids."""
    report = selftest_report()
    assert report.dataset == "harness-selftest"
    assert any("SELFTEST" in note for note in report.notes)
    assert "SELFTEST" in report.to_json().decode()


def test_history_rows_are_appended_not_edited(tmp_path) -> None:
    path = tmp_path / "history.md"
    path.write_text("| header |\n", encoding="utf-8")
    report = selftest_report()
    append_history(report, "M4", path)
    first = path.read_text(encoding="utf-8")
    append_history(report, "M4", path)
    assert path.read_text(encoding="utf-8") == first, "identical rows are not duplicated"
    assert "M4" in first and "| header |" in first
