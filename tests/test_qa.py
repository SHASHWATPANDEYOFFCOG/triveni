"""M15 gate: grounded Q&A, numeral verification, and abstention.

The DoD is an adversarial question set where every unanswerable question abstains and
zero figures are fabricated. Two properties carry that:

* the model never writes SQL - it picks from templates we wrote, and its parameters
  are validated, so the worst an adversarial response can do is run one of our own
  queries with the wrong arguments;
* every numeral in the answer is checked against the values SQL returned, and an
  answer containing one that is not there is *discarded*, not repaired.

The tests below include the failure that mattered most: a topic gate, without which
"How much did we lose to fraud?" matched on the words "how much" and answered with a
real cash total - a true number about the wrong question, which is worse than a refusal.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from qa.compile import (
    TEMPLATES,
    Intent,
    assert_reads_only_allowed_views,
    catalogue,
    compile_question,
)
from qa.narrate import (
    answer_question,
    describe_rows,
    verify_grounding,
)
from qa.warehouse import ALLOWED_VIEWS, build
from recon.pipeline import reconcile
from scripts.qa_adversarial import ADVERSARIAL, ANSWERABLE

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def warehouse():
    return build(reconcile())


# --------------------------------------------------------------------------- #
# The DoD: the adversarial set
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", ADVERSARIAL, ids=lambda c: (c.question[:40] or "empty"))
def test_every_unanswerable_question_abstains(case, warehouse) -> None:
    """Zero fabricated figures means zero, including for questions engineered to
    produce one."""
    answer = answer_question(case.question, warehouse)
    assert not answer.answered, f"answered the unanswerable: {case.question!r}"
    assert answer.reason, "an abstention without a reason is just a shrug"


@pytest.mark.parametrize("case", ANSWERABLE, ids=lambda c: c.question[:40])
def test_answerable_questions_are_answered_and_fully_traced(case, warehouse) -> None:
    """A system that abstains on everything is not grounded, it is mute - and those
    are very different products. The suite must not be passable by refusing."""
    answer = answer_question(case.question, warehouse)
    assert answer.answered, f"could not answer: {case.question!r}"
    assert answer.grounding is not None and answer.grounding.grounded
    assert answer.grounding.ungrounded == ()
    assert answer.rows, "an answer with no rows behind it has nothing to be grounded in"


def test_the_adversarial_script_passes_as_a_whole() -> None:
    from scripts.qa_adversarial import main

    assert main([]) == 0


# --------------------------------------------------------------------------- #
# The topic gate - the bug that mattered
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question",
    [
        "How much did we lose to fraud?",
        "Roughly what percentage of merchants have this problem?",
        "How much is our market share?",
        "What is the capital of France?",
    ],
)
def test_a_generic_quantifier_is_not_an_intent(question: str, warehouse) -> None:
    """"How much" and "problem" are not topics.

    Without a topic gate these matched CASH_TOTAL and EXCEPTIONS_BY_TYPE respectively
    and returned real figures about something else entirely - which is worse than a
    refusal, because the number is correct and the answer is wrong.
    """
    compiled = compile_question(question)
    assert compiled.intent is Intent.UNSUPPORTED, question
    assert answer_question(question, warehouse).answered is False


@pytest.mark.parametrize(
    "question",
    [
        "How many exceptions are there?",
        "What were the fees?",
        "Show me the settlements",
        "How much cash landed?",
        "Which refunds were netted?",
    ],
)
def test_the_topic_gate_admits_plurals(question: str) -> None:
    """An anchored alternation rejects 'exceptions' while accepting 'exception'. That
    silently blocked half the questions the gate was meant to admit."""
    assert compile_question(question).intent is not Intent.UNSUPPORTED, question


@pytest.mark.parametrize(
    "question",
    [
        "Estimate the settlements we probably missed.",
        "What will next quarter's settlement volume be?",
        "Roughly how much will we receive?",
        "Predict the exceptions for next month.",
    ],
)
def test_speculation_is_refused_even_on_an_in_scope_topic(question: str) -> None:
    """A guess is still a guess when it is about settlements. The reconciled ledger
    holds what happened; forward-looking cash goes through the forecast, which ships
    its own measured interval."""
    compiled = compile_question(question)
    assert compiled.intent is Intent.UNSUPPORTED
    assert "estimate" in compiled.refusal or "projection" in compiled.refusal


# --------------------------------------------------------------------------- #
# The model never writes SQL
# --------------------------------------------------------------------------- #
def test_every_template_is_hand_written_and_readable() -> None:
    for intent, template in TEMPLATES.items():
        assert template.intent is intent
        assert template.description
        assert "SELECT" in template.sql.upper()
        assert len(template.sql.splitlines()) < 20, f"{intent} is too long to review"


def test_templates_only_read_the_documented_views() -> None:
    """Three views, nothing else. There is no table here to join wrongly."""
    for intent, template in TEMPLATES.items():
        arguments = {"settlement_id": "setl_x", "as_of": "2026-03-04", "limit": 10}
        sql = template.render({k: arguments[k] for k in template.parameters})
        assert_reads_only_allowed_views(sql)


def test_a_query_touching_anything_else_is_refused() -> None:
    with pytest.raises(ValueError, match="non-view tables"):
        assert_reads_only_allowed_views("SELECT * FROM users")
    with pytest.raises(ValueError, match="non-view tables"):
        assert_reads_only_allowed_views("SELECT * FROM v_exceptions JOIN secrets ON 1=1")


def test_parameters_are_validated_before_reaching_a_template() -> None:
    """The point at which a question stops being able to influence the query."""
    assert compile_question("show me settlement setl_601010000489").supported
    # A settlement id shaped like an injection never reaches SQL.
    bad = compile_question("show me settlement setl_x'; DROP TABLE v_exceptions;--")
    if bad.supported:
        assert "'" not in bad.arguments.get("settlement_id", "")
        assert "DROP" not in bad.sql.upper()


def test_an_out_of_range_limit_is_refused() -> None:
    from qa.compile import _validate

    with pytest.raises(ValueError, match="out of range"):
        _validate(Intent.LARGEST_EXCEPTIONS, {"limit": 9999})


def test_a_malformed_date_is_refused() -> None:
    from qa.compile import _validate

    with pytest.raises(ValueError):
        _validate(Intent.SETTLEMENT_ON_DATE, {"as_of": "not-a-date"})


def test_a_model_that_invents_an_intent_is_refused() -> None:
    """The blast radius of an adversarial model response is bounded by the enum.

    Exercised against `_ask_model` directly rather than through `compile_question`,
    because the deterministic patterns answer almost everything in scope before the
    model is ever consulted - which is the design working, but makes it impossible to
    reach the model path through the front door with a realistic question.
    """
    from qa.compile import _ask_model

    class Rogue:
        def call(self, prompt_name, variables, **kwargs):
            class Outcome:
                abstained = False
                data = {"intent": "DROP_EVERYTHING"}

            return Outcome()

    assert _ask_model("anything at all", Rogue()) is None


def test_a_model_choosing_a_real_intent_still_has_its_arguments_validated() -> None:
    """It may pick one of our queries. It may not choose the parameters freely."""
    from qa.compile import _ask_model

    class Sneaky:
        def call(self, prompt_name, variables, **kwargs):
            class Outcome:
                abstained = False
                data = {
                    "intent": "settlement_detail",
                    "settlement_id": "x'; DROP TABLE v_exceptions;--",
                }

            return Outcome()

    compiled = _ask_model("show me a settlement", Sneaky())
    assert compiled is not None
    assert compiled.intent is Intent.UNSUPPORTED
    assert "parameter" in compiled.refusal


def test_the_catalogue_documents_what_can_be_answered() -> None:
    entries = catalogue()
    assert len(entries) == len(TEMPLATES)
    assert all(entry["answers"] for entry in entries)


def test_the_view_file_and_the_allowlist_agree() -> None:
    sql = (ROOT / "qa" / "semantic_view.sql").read_text(encoding="utf-8")
    for view in ALLOWED_VIEWS:
        assert f"CREATE OR REPLACE VIEW {view}" in sql


# --------------------------------------------------------------------------- #
# Numeral grounding
# --------------------------------------------------------------------------- #
def test_a_fabricated_figure_is_caught() -> None:
    result = verify_grounding("The total is 999999 paise.", ["total_paise"], [(500000,)])
    assert not result.grounded
    assert "999999" in result.ungrounded


def test_a_truthful_figure_passes() -> None:
    assert verify_grounding("The total is 500000 paise.", ["p"], [(500000,)]).grounded


def test_a_rupee_restatement_of_a_paise_column_passes() -> None:
    """Rejecting this would make the checker useless on the only output anyone wants."""
    assert verify_grounding("The total is Rs 5000.", ["p"], [(500000,)]).grounded
    assert verify_grounding("The total is 5,000.00 rupees.", ["p"], [(500000,)]).grounded


def test_a_date_is_checked_as_a_date_not_as_three_numbers() -> None:
    """`2026-03-04` left in the text yields the 'numerals' 2026, -03 and -04, none of
    which appears in any row - discarding a perfectly good answer for quoting a date
    the query itself returned."""
    rows = [(dt.date(2026, 3, 4), 500000)]
    assert verify_grounding("Settled on 2026-03-04 for 500000 paise.", ["d", "p"], rows).grounded

    wrong = verify_grounding("Settled on 2026-09-09 for 500000 paise.", ["d", "p"], rows)
    assert not wrong.grounded
    assert "2026-09-09" in wrong.ungrounded


def test_row_and_column_counts_are_quotable() -> None:
    """"3 settlements did not balance" is a claim about the result set itself."""
    assert verify_grounding("3 rows returned.", ["a"], [(1,), (2,), (3,)]).grounded


def test_statutory_constants_need_no_source() -> None:
    """18% GST and section 194-O are fixed in law, not findings from this data."""
    result = verify_grounding("GST is 18% under section 194-O.", ["p"], [(500000,)])
    assert result.grounded


def test_an_ungrounded_narration_is_discarded_not_repaired(warehouse) -> None:
    """The whole answer goes, and the user gets the table. A sentence that is 90%
    right about money is worse than no sentence."""

    class Fabricator:
        def call(self, prompt_name, variables, **kwargs):
            class Outcome:
                abstained = False
                data = {"answer": "The gateway withheld exactly 8675309 paise in fees."}

            return Outcome()

    answer = answer_question(
        "how much did the gateway take in fees?", warehouse, gateway=Fabricator()
    )
    assert not answer.answered
    assert "8675309" not in answer.text
    assert answer.rows, "the table must still be shown"
    assert answer.grounding is not None and "8675309" in answer.grounding.ungrounded


def test_an_empty_result_set_abstains_rather_than_narrating_nothing(warehouse) -> None:
    answer = answer_question("show me settlement setl_NOTHING", warehouse)
    assert not answer.answered
    assert "invented" in answer.reason or "no row" in answer.reason.lower()


def test_the_deterministic_narration_cannot_be_ungrounded(warehouse) -> None:
    """It is assembled from the values themselves, so it is the floor an LLM
    narration has to beat rather than a fallback that might be worse."""
    for case in ANSWERABLE:
        answer = answer_question(case.question, warehouse)
        assert answer.grounding is not None and answer.grounding.grounded


def test_describe_rows_handles_the_empty_case() -> None:
    assert "no rows" in describe_rows(["a"], [])


# --------------------------------------------------------------------------- #
# The warehouse
# --------------------------------------------------------------------------- #
def test_the_warehouse_exposes_only_three_views(warehouse) -> None:
    assert set(warehouse.row_counts) == ALLOWED_VIEWS
    assert warehouse.row_counts["v_settlements"] > 0
    assert warehouse.row_counts["v_exceptions"] > 0


def test_every_settlement_row_carries_its_waterfall(warehouse) -> None:
    columns, rows = warehouse.query(
        "SELECT gross_paise, mdr_paise, gst_paise, tds_paise, net_received_paise, "
        "residual_paise, balanced FROM v_settlements"
    )
    assert rows
    for row in rows:
        gross, mdr, gst, tds, received, residual, balanced = row
        assert gross >= 0 and received >= 0
        assert mdr >= 0 and gst >= 0 and tds >= 0
        if balanced:
            assert abs(residual) <= 100, "a balanced settlement has no material residual"


def test_the_schema_handed_to_the_model_is_the_documented_one(warehouse) -> None:
    """A model that cannot see a column cannot invent a query against it."""
    schema = warehouse.schema()
    assert "v_settlements" in schema and "v_exceptions" in schema
    assert "-- " in schema, "every column is documented in the file the model sees"


def test_answers_serialise_for_the_api(warehouse) -> None:
    answer = answer_question("how much did the gateway take in fees?", warehouse)
    payload = answer.canonical()
    assert payload["answered"] is True
    assert payload["grounding"]["grounded"] is True
    assert payload["sql"].upper().startswith("SELECT")


def test_qa_is_deterministic(warehouse) -> None:
    question = "what are the top 5 largest exceptions?"
    first = answer_question(question, warehouse)
    second = answer_question(question, warehouse)
    assert first.text == second.text
    assert first.canonical() == second.canonical()
