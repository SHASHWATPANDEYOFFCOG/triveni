"""M9 gate: global assignment, many-to-one netting, and the balance invariants.

The DoD is a property test proving no double-booking, and the conservation invariant
holding to the paise on the full batch. Both are here.

The tests also pin the four modelling errors this milestone produced, every one of
which satisfied its constraints while being wrong - which is exactly why a solver
needs tests about its *objective*, not just its feasibility.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.money import Money
from ingest.canonical import PaymentMethod
from recon.assign import (
    Bucket,
    assign_many_to_one,
    assign_one_to_one,
    check_conservation,
    check_no_double_use,
)
from recon.fees import DEFAULT_RATE_CARD, RateCard
from recon.pipeline import reconcile
from recon.subsetsum import (
    MITM_LIMIT,
    Tolerance,
    has_cp_sat,
    meet_in_the_middle,
    solve,
    whole_window,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def result():
    return reconcile()


# --------------------------------------------------------------------------- #
# The DoD: no double-booking
# --------------------------------------------------------------------------- #
def test_no_row_is_claimed_twice_for_the_same_relation(result) -> None:
    """Invariant D.1.3, stated per relation.

    A payment genuinely participates in two relations - it pays an invoice, and it
    settles into a bank credit - so the literal row-level rule is wrong here. Reading
    it literally made Stage 4 absorb Stage 1's certain invoice links into an uncertain
    settlement attribution and drop their confidence from 1.00 to 0.85, taking the
    auto-postable share of the entire batch to zero.
    """
    assert result.check_no_double_spend() == []


def test_an_invoice_is_paid_by_at_most_one_payment(result) -> None:
    seen: dict[str, str] = {}
    for group in result.matches:
        for gateway_id, ledger_id in group.links:
            assert ledger_id not in seen, f"{ledger_id} paid by two payments"
            seen[ledger_id] = gateway_id


def test_a_payment_settles_into_at_most_one_credit(result) -> None:
    """The constraint the whole CP-SAT model exists to enforce."""
    seen: dict[str, str] = {}
    for group in result.matches:
        if not group.bank_ids:
            continue
        for gateway_id in group.gateway_ids:
            assert gateway_id not in seen, f"{gateway_id} settled into two credits"
            seen[gateway_id] = group.bank_ids[0]


@given(
    amounts=st.lists(st.integers(min_value=1000, max_value=500_000), min_size=2, max_size=12),
    n_buckets=st.integers(min_value=2, max_value=4),
)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_solver_never_double_books_whatever_the_input(
    amounts: list[int], n_buckets: int
) -> None:
    """The property test the DoD asks for. Every payment is offered to every bucket,
    which is the most adversarial shape there is."""
    payments = {f"p{i}": value for i, value in enumerate(amounts)}
    buckets = [
        Bucket(f"b{j}", sum(amounts) // n_buckets, tuple(payments), Tolerance(lower=10**9, upper=0))
        for j in range(n_buckets)
    ]
    outcome = assign_many_to_one(buckets, payments, deterministic_budget=1.0)
    assert check_no_double_use(outcome.groups) == []


# --------------------------------------------------------------------------- #
# The DoD: conservation to the paise
# --------------------------------------------------------------------------- #
def test_conservation_is_exact_not_approximate() -> None:
    """Integer paise throughout, so 'exactly' means exactly. A residual is not noise
    to absorb; it is the unexplained money, and it belongs in the exception queue."""
    check = check_conservation(
        matched_gross=Money.from_rupees("1240000"),
        explained=Money.from_rupees("29041"),
        matched_net=Money.from_rupees("1210959"),
    )
    assert check.holds and check.residual.is_zero

    off_by_one_paise = check_conservation(
        matched_gross=Money(124_000_000),
        explained=Money(2_904_100),
        matched_net=Money(121_095_901),
    )
    assert not off_by_one_paise.holds
    assert off_by_one_paise.residual.paise == -1


def test_conservation_holds_for_every_settlement_group_on_the_full_batch(result) -> None:
    """gross - withheld == the credit that landed, for every Stage 4 group, to the
    paise. Checked against the group's own evidence rather than recomputed, so a
    reason string that disagrees with the arithmetic fails here."""
    groups = [m for m in result.matches if m.stage.startswith("stage4")]
    assert groups, "Stage 4 produced no settlement groups"

    for group in groups:
        gross = next(
            i for i in group.evidence.items if i.label == "gross of the chosen subset"
        )
        credit = next(i for i in group.evidence.items if i.label == "credit that landed")
        withheld = next(i for i in group.evidence.items if i.label.startswith("withheld"))
        check = check_conservation(
            matched_gross=Money.parse(gross.detail),
            explained=Money.parse(withheld.detail),
            matched_net=Money.parse(credit.detail),
        )
        assert check.holds, f"{group.match_id}: {check.render()}"


def test_money_never_appears_from_nowhere(result) -> None:
    """A credit can only be *less* than the gross that produced it - every adjustment
    in the chain is a deduction. The tolerance band was inverted at one point and the
    solver returned settlements where the bank paid more than the merchant sold."""
    for group in result.matches:
        if not group.stage.startswith("stage4"):
            continue
        withheld = next(i for i in group.evidence.items if i.label.startswith("withheld"))
        assert Money.parse(withheld.detail).paise >= 0, group.reason


def test_the_tolerance_band_is_one_sided() -> None:
    tolerance = Tolerance.from_rate(1_000_000)
    low, high = tolerance.bounds(1_000_000)
    assert low <= 1_000_000 <= high
    assert high - 1_000_000 > 1_000_000 - low, "deductions are large, windfalls are not"
    assert not tolerance.contains(900_000, 1_000_000)
    assert tolerance.contains(1_020_000, 1_000_000)


# --------------------------------------------------------------------------- #
# Global optimality, not local plausibility
# --------------------------------------------------------------------------- #
def test_optimal_assignment_beats_greedy() -> None:
    """The worked example from the module docstring. Greedy takes the best pair first
    and strands the rest; it does not lose a little precision, it loses a whole match."""
    scores = {("A", "X"): 9.0, ("A", "Y"): 8.8, ("B", "X"): 8.9}
    outcome = assign_one_to_one(["A", "B"], ["X", "Y"], scores)
    assert abs(outcome.total_score - 17.7) < 1e-9
    assert {(p.left, p.right) for p in outcome.pairs} == {("A", "Y"), ("B", "X")}


def test_abstention_is_a_priced_option_not_an_afterthought() -> None:
    """Setting the no-match price to the accept threshold puts the threshold *inside*
    the optimisation, so a row is matched only when matching beats declining."""
    weak = {("A", "X"): 2.0}
    assert assign_one_to_one(["A"], ["X"], weak, no_match_price=0.0).pairs
    assert not assign_one_to_one(["A"], ["X"], weak, no_match_price=5.0).pairs


def test_the_same_payment_cannot_serve_two_settlements() -> None:
    amounts = {"p1": 1000, "p2": 2000, "p3": 3000, "p4": 4000}
    buckets = [
        Bucket("A", 3000, tuple(amounts), Tolerance(lower=0, upper=0)),
        Bucket("B", 3000, tuple(amounts), Tolerance(lower=0, upper=0)),
    ]
    # attribution_weight=1 is the conservative end of the dial: attribute a payment
    # only when it clearly fits. It is what makes the exact-balance assertion below
    # meaningful; at the shipped default of 3 the solver would rather place p4
    # somewhere and carry the residual, which is the right behaviour on real data and
    # the wrong behaviour to assert exactness against.
    outcome = assign_many_to_one(buckets, amounts, attribution_weight=1, deterministic_budget=2.0)
    assert check_no_double_use(outcome.groups) == []
    assert len(outcome.groups) == 2
    assert all(g.residual == 0 for g in outcome.groups)


def test_the_objective_does_not_telescope_to_a_constant() -> None:
    """Minimising the *signed* residual sum is silently useless: once every payment is
    attributed, it is a constant, and the solver returns an arbitrary consistent
    answer. 68% of payments landed in the wrong settlement while every hard constraint
    held. The absolute value is what makes a wrong bucket cost something."""
    amounts = {"a": 1000, "b": 3000}
    buckets = [
        Bucket("early", 1000, ("a", "b"), Tolerance(lower=10_000, upper=0)),
        Bucket("late", 3000, ("a", "b"), Tolerance(lower=10_000, upper=0)),
    ]
    outcome = assign_many_to_one(buckets, amounts, deterministic_budget=2.0)
    placed = {m: g.bucket_id for g in outcome.groups for m in g.member_ids}
    assert placed == {"a": "early", "b": "late"}


def test_pair_weights_steer_the_attribution() -> None:
    """The settlement calendar must be *in the objective*, not merely in the candidate
    set. Amount alone is nearly flat between adjacent days, so without a date penalty
    the solver found low-residual attributions that were wrong."""
    amounts = {"a": 1000, "b": 1000}
    buckets = [
        Bucket("mon", 1000, ("a", "b"), Tolerance(lower=5_000, upper=0)),
        Bucket("tue", 1000, ("a", "b"), Tolerance(lower=5_000, upper=0)),
    ]
    weights = {("a", "mon"): 0.0, ("a", "tue"): 1e6, ("b", "mon"): 1e6, ("b", "tue"): 0.0}
    outcome = assign_many_to_one(buckets, amounts, weights=weights, deterministic_budget=2.0)
    placed = {m: g.bucket_id for g in outcome.groups for m in g.member_ids}
    assert placed == {"a": "mon", "b": "tue"}


# --------------------------------------------------------------------------- #
# Subset-sum
# --------------------------------------------------------------------------- #
def test_meet_in_the_middle_agrees_with_brute_force() -> None:
    import itertools

    items = [1000, 2500, 3300, 4100, 5500, 6700, 7900, 8800, 9100, 10200, 11300, 12400]
    exact = Tolerance(lower=0, upper=0)
    for target in range(1000, 40000, 971):
        found = meet_in_the_middle(items, target, exact)
        brute = any(
            sum(items[i] for i in combo) == target
            for k in range(len(items) + 1)
            for combo in itertools.combinations(range(len(items)), k)
        )
        assert (found is not None) == brute, target
        if found:
            assert found.total == target


def test_whole_window_is_tried_first_and_costs_one_addition() -> None:
    """A settlement is normally the whole day's captures, so checking that before any
    search resolves most days immediately."""
    items = [100, 200, 300]
    outcome = whole_window(items, 600, Tolerance(lower=0, upper=0))
    assert outcome is not None
    assert outcome.method == "whole-window"
    assert outcome.searched == 1
    assert outcome.indices == (0, 1, 2)


def test_the_search_reports_the_space_it_pruned() -> None:
    """A throughput claim that is not measured is not a claim."""
    items = list(range(1000, 1000 + 12 * 137, 137))
    outcome = solve(items, sum(items[:5]), Tolerance(lower=0, upper=0))
    assert outcome is not None
    assert outcome.space == Decimal(2) ** len(items)
    assert outcome.searched < int(outcome.space)


def test_an_impossible_target_is_refused_not_approximated() -> None:
    assert solve([100, 200], 999_999, Tolerance(lower=10, upper=10)) is None


def test_too_many_candidates_raises_rather_than_hanging() -> None:
    """An unbounded search turns a reconciliation into a spinner. 'Blocking did not
    constrain this' is more useful to an operator."""
    from core.errors import SolverError

    with pytest.raises(SolverError, match="blocking"):
        solve([1] * 500, 10, Tolerance(lower=1))


def test_meet_in_the_middle_refuses_beyond_its_limit() -> None:
    from core.errors import SolverError

    with pytest.raises(SolverError):
        meet_in_the_middle([1] * (MITM_LIMIT + 1), 5, Tolerance(lower=1))


def test_cp_sat_is_available_and_used() -> None:
    """Verified at M0 before anything depended on it; asserted here so a missing
    ortools surfaces as a failing test rather than a silent quality regression."""
    assert has_cp_sat()


# --------------------------------------------------------------------------- #
# The rate card
# --------------------------------------------------------------------------- #
def test_the_fee_decomposition_balances_to_the_paise() -> None:
    gross = Money.from_rupees("1240000")
    breakdown = DEFAULT_RATE_CARD.decompose(gross, PaymentMethod.CARD_CREDIT)
    assert breakdown.balances()
    assert breakdown.gross.paise - breakdown.total_deducted().paise == breakdown.net.paise


def test_zero_mdr_methods_are_charged_no_fee_but_still_pay_tds() -> None:
    """UPI and RuPay debit are zero-rated by regulation; TDS is on the sale, not the
    fee, so it applies anyway."""
    gross = Money.from_rupees("10000")
    for method in (PaymentMethod.UPI, PaymentMethod.RUPAY_DEBIT):
        breakdown = DEFAULT_RATE_CARD.decompose(gross, method)
        by_name = {d.component: d.amount for d in breakdown.deductions}
        assert by_name["fee_mdr"].is_zero
        assert by_name["gst_on_fee"].is_zero
        assert by_name["tds_194o"].paise > 0


def test_gst_is_charged_on_the_fee_not_the_sale() -> None:
    """Charging it on gross overstates the deduction by roughly fifty times."""
    gross = Money.from_rupees("100000")
    breakdown = DEFAULT_RATE_CARD.decompose(gross, PaymentMethod.CARD_CREDIT)
    by_name = {d.component: d.amount for d in breakdown.deductions}
    assert by_name["gst_on_fee"].paise == round(by_name["fee_mdr"].paise * 0.18)
    assert "not of the sale" in next(
        d.basis for d in breakdown.deductions if d.component == "gst_on_fee"
    )


def test_the_rate_card_is_configurable() -> None:
    card = RateCard(mdr_bps={**DEFAULT_RATE_CARD.mdr_bps, PaymentMethod.UPI: 50})
    gross = Money.from_rupees("10000")
    assert card.decompose(gross, PaymentMethod.UPI).net != DEFAULT_RATE_CARD.decompose(
        gross, PaymentMethod.UPI
    ).net


# --------------------------------------------------------------------------- #
# In the pipeline
# --------------------------------------------------------------------------- #
def test_stage_four_runs_and_attributes_every_credit(result) -> None:
    assert [s.stage for s in result.stages] == ["stage0", "stage1", "stage2", "stage3", "stage4", "stage5", "stage6"]
    assert result.netting is not None
    # Payments the solver declined to place are *reported*, never dropped: attributing
    # one would have created more unexplained money than the payment is worth, and
    # saying so is the honest answer.
    from recon.exceptions import ExceptionType

    unattributed = {e.source_ids[0] for e in result.exceptions
                    if e.exception_type is ExceptionType.MISSING_IN_BANK}
    assert set(result.netting.unassigned) <= unattributed
    attributed = sum(len(g.member_ids) for g in result.netting.groups)
    offered = attributed + len(result.netting.unassigned)
    assert attributed / offered > 0.6, (
        f"only {attributed}/{offered} payments placed; the solver is declining too much"
    )


def test_stage_four_reports_whether_it_proved_optimality(result) -> None:
    """Optimality and correctness are different claims. The hard invariant holds under
    feasibility; optimality only picks among consistent attributions."""
    detail = next(s for s in result.stages if s.stage == "stage4").detail
    assert "proven optimal" in detail or "not proven optimal" in detail


def test_stage_four_does_not_republish_stage_one_links(result) -> None:
    """Merging them here would reassert certain claims at this stage's lower
    confidence and stop them being auto-postable."""
    for group in result.matches:
        if group.stage.startswith("stage4"):
            assert group.links == ()


def test_certain_matches_stay_certain(result) -> None:
    stage1 = [m for m in result.matches if m.stage.startswith("stage1")]
    assert stage1
    assert all(m.confidence == Decimal(1) for m in stage1)


def test_an_unexplained_credit_becomes_a_finding(result) -> None:
    """A credit nobody can explain is the most important thing on the page. It must
    not simply stay unmatched and silent."""
    from ingest.canonical import SourceKind
    from recon.exceptions import ExceptionType

    unmatched = {r.txn_id for r in result.unmatched(SourceKind.BANK)}
    reported = {
        e.source_ids[0]
        for e in result.exceptions
        if e.exception_type is ExceptionType.MISSING_IN_LEDGER
    }
    assert unmatched <= reported, sorted(unmatched - reported)
    for exception in result.exceptions:
        if exception.exception_type is ExceptionType.MISSING_IN_LEDGER:
            assert exception.evidence.abstained_because
            assert exception.suggested_action


def test_the_solver_is_deterministic() -> None:
    """CP-SAT is given a *deterministic* budget, not a wall-clock one.

    With `max_time_in_seconds` the answer depended on how fast the machine happened to
    be that second - two runs of the same reconciliation returned different
    attributions because the timeout landed in a different place. That would have
    quietly broken `make eval`'s byte-identical guarantee for anyone whose laptop was
    busy.

    The gateway reset is not a way of making this pass; it removes a variable that has
    nothing to do with the solver. The LLM budget is deliberately *process-wide* - it
    caps spend per run - so by the time the full suite reaches this test the cap is
    partly spent, and it can fall between the two reconciliations below. That made this
    test fail for a reason its name does not describe, and the failure was in the
    exception list rather than the match list, which is the signature of the escalation
    stage abstaining, not of the solver wobbling. The property that budget exhaustion
    is *safe* is worth asserting on its own, and is asserted directly below.
    """
    from core.llm import reset_gateway

    reset_gateway()
    first = reconcile()
    reset_gateway()
    second = reconcile()
    assert [m.match_id for m in first.matches] == [m.match_id for m in second.matches]
    assert [e.exception_id for e in first.exceptions] == [
        e.exception_id for e in second.exceptions
    ]


def test_running_out_of_llm_budget_never_changes_a_money_decision() -> None:
    """Exhausting the budget must cost coverage, never correctness.

    Found while diagnosing the flake above: with the cap drained, a reconciliation
    produces a *different exception list* - more rows abstain and route to a human -
    while the match list stays byte-identical. That is the direction a finance system
    is allowed to degrade in, and it is the reason the flake was a test bug rather than
    a product bug. Pinned here because nothing else asserts it, and the opposite
    behaviour - a spent budget quietly changing which payments were matched to which
    settlement - would be the most dangerous failure this system could have.
    """
    from core.llm import LLMGateway, reset_gateway

    reset_gateway()
    funded = reconcile()

    reset_gateway(LLMGateway.from_env())
    from core.llm import gateway

    gateway().budget_inr = Decimal("0.0001")
    starved = reconcile()

    assert [m.match_id for m in starved.matches] == [
        m.match_id for m in funded.matches
    ], "a spent LLM budget must not change a single match"
    assert gateway().meter.abstentions > 0, (
        "expected the drained budget to force abstentions - if it did not, this test "
        "is no longer exercising the condition it describes"
    )

    reset_gateway()


def test_auto_posted_matches_are_perfectly_precise() -> None:
    """The number that actually governs the cost model: nothing wrong is posted
    without a human. Coverage is allowed to be low; unsupervised error is not."""
    from scripts.eval_pipeline import evaluate_pipeline

    report = evaluate_pipeline()
    by_name = {m.name: m for m in report.metrics}
    assert by_name["auto_post_precision"].value == 1
    assert by_name["auto_post_coverage"].value > 0
    # M8 (no netting) reached 0.3475. Stage 4 roughly doubles it. It does not reach
    # 1.0 and should not: the objective declines an attribution that would create more
    # unexplained money than the payment is worth, which trades recall for precision
    # deliberately - 0.7699 -> 0.8679 precision when that rule was added.
    assert by_name["recall"].value > Decimal("0.60"), "Stage 4 must lift recall materially"
    assert by_name["precision"].value > Decimal("0.80")
