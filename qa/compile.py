"""Natural language to SQL, without letting a model write SQL.

The usual text-to-SQL design hands a model a schema and asks for a query. That is the
wrong shape for a finance tool. A free-form query can join across a boundary that makes
no sense, aggregate a column whose meaning it guessed, or - in the worst case - be
steered by a question that is really an instruction.

So Triveni does not generate SQL. It **selects a template and fills its parameters**.

    question -> intent + parameters -> a template we wrote -> execute -> rows

The templates are in this file, written by hand, each one a query a reviewer can read
in ten seconds. The model's entire job is to pick one and extract its arguments, and
the arguments are validated - a date must parse, an exception type must be one of the
sixteen, a limit must be an integer in range. A question the templates cannot answer
returns `UNSUPPORTED`, and the honest answer is "I cannot answer that from the data",
not a query that runs and returns something plausible.

Selection is deterministic first. Most questions a merchant asks are recognisable by
pattern, and pattern-matching costs nothing and cannot be steered. Only genuinely
ambiguous phrasing reaches the model - and the model is choosing from a closed set of
templates, so even a fully adversarial response can only pick a query we already wrote.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from qa.warehouse import ALLOWED_VIEWS
from recon.exceptions import ExceptionType


class Intent(StrEnum):
    """The closed set of questions Triveni can answer from reconciled data."""

    SETTLEMENT_DETAIL = "settlement_detail"
    """Why is settlement X short / what is in it?"""

    SETTLEMENT_ON_DATE = "settlement_on_date"
    """What settled on a given day?"""

    UNBALANCED_SETTLEMENTS = "unbalanced_settlements"
    """Which settlements did not close to zero?"""

    ALL_SETTLEMENTS = "all_settlements"
    """Just show me the settlements. Added because "show me the settlements" is an
    obvious question that fell through every other pattern - SETTLEMENT_DETAIL matched
    on the word and then failed validation for want of an id, and the fallthrough was
    an unhelpful refusal to a perfectly reasonable request."""

    EXCEPTIONS_BY_TYPE = "exceptions_by_type"
    """How many exceptions of each type, or of one type?"""

    EXCEPTIONS_BY_SEVERITY = "exceptions_by_severity"
    """What needs a human most urgently?"""

    LARGEST_EXCEPTIONS = "largest_exceptions"
    """Which exceptions are worth the most money?"""

    TOTAL_FEES = "total_fees"
    """How much did the gateway withhold, and in what?"""

    CASH_BY_DAY = "cash_by_day"
    """How much landed per day?"""

    CASH_TOTAL = "cash_total"
    """How much landed in total, over a period?"""

    UNSUPPORTED = "unsupported"
    """Nothing here can answer it. The honest outcome, not a failure."""


@dataclass(frozen=True, slots=True)
class Template:
    """One hand-written query. The model may choose it; it may never edit it."""

    intent: Intent
    sql: str
    description: str
    parameters: tuple[str, ...] = ()

    def render(self, arguments: dict[str, Any]) -> str:
        """Substitute validated parameters. Every value has already been through
        :func:`_validate`, so nothing unchecked reaches the string."""
        return self.sql.format(**arguments)


#: The whole surface. Nine queries, each readable in ten seconds.
TEMPLATES: Final[dict[Intent, Template]] = {
    Intent.SETTLEMENT_DETAIL: Template(
        Intent.SETTLEMENT_DETAIL,
        """
        SELECT settlement_id, settled_on, gross_paise, mdr_paise, gst_paise,
               tds_paise, refunds_paise, chargeback_paise, reserve_paise,
               net_expected_paise, net_received_paise, residual_paise, balanced
        FROM v_settlements
        WHERE settlement_id = '{settlement_id}'
        """,
        "the full waterfall for one settlement",
        ("settlement_id",),
    ),
    Intent.SETTLEMENT_ON_DATE: Template(
        Intent.SETTLEMENT_ON_DATE,
        """
        SELECT settlement_id, settled_on, gross_paise, net_received_paise,
               residual_paise, balanced, member_count
        FROM v_settlements
        WHERE settled_on = DATE '{as_of}'
        ORDER BY settlement_id
        """,
        "settlements credited on a given date",
        ("as_of",),
    ),
    Intent.UNBALANCED_SETTLEMENTS: Template(
        Intent.UNBALANCED_SETTLEMENTS,
        """
        SELECT settlement_id, settled_on, residual_paise, gross_paise, member_count
        FROM v_settlements
        WHERE balanced = FALSE
        ORDER BY ABS(residual_paise) DESC
        LIMIT {limit}
        """,
        "settlements whose waterfall did not close to zero",
        ("limit",),
    ),
    Intent.ALL_SETTLEMENTS: Template(
        Intent.ALL_SETTLEMENTS,
        """
        SELECT settlement_id, settled_on, gross_paise, net_received_paise,
               residual_paise, balanced, member_count
        FROM v_settlements
        ORDER BY settled_on, settlement_id
        LIMIT {limit}
        """,
        "every settlement, with whether its waterfall balanced",
        ("limit",),
    ),
    Intent.EXCEPTIONS_BY_TYPE: Template(
        Intent.EXCEPTIONS_BY_TYPE,
        """
        SELECT exception_type, COUNT(*) AS exception_count,
               SUM(amount_paise) AS total_paise
        FROM v_exceptions
        GROUP BY exception_type
        ORDER BY exception_count DESC
        """,
        "how many exceptions of each type, and what they are worth",
    ),
    Intent.EXCEPTIONS_BY_SEVERITY: Template(
        Intent.EXCEPTIONS_BY_SEVERITY,
        """
        SELECT severity, COUNT(*) AS exception_count,
               SUM(amount_paise) AS total_paise
        FROM v_exceptions
        GROUP BY severity
        ORDER BY exception_count DESC
        """,
        "what needs a human, by urgency",
    ),
    Intent.LARGEST_EXCEPTIONS: Template(
        Intent.LARGEST_EXCEPTIONS,
        """
        SELECT exception_id, exception_type, severity, amount_paise, reason
        FROM v_exceptions
        ORDER BY ABS(amount_paise) DESC
        LIMIT {limit}
        """,
        "the exceptions worth the most money",
        ("limit",),
    ),
    Intent.TOTAL_FEES: Template(
        Intent.TOTAL_FEES,
        """
        SELECT SUM(mdr_paise) AS mdr_paise, SUM(gst_paise) AS gst_paise,
               SUM(tds_paise) AS tds_paise, SUM(refunds_paise) AS refunds_paise,
               SUM(chargeback_paise) AS chargeback_paise,
               SUM(reserve_paise) AS reserve_paise,
               SUM(gross_paise) AS gross_paise,
               SUM(net_received_paise) AS net_received_paise
        FROM v_settlements
        """,
        "everything the gateway withheld, by component",
    ),
    Intent.CASH_BY_DAY: Template(
        Intent.CASH_BY_DAY,
        """
        SELECT value_date, credited_paise, credit_count, is_business_day
        FROM v_daily_cash
        ORDER BY value_date
        LIMIT {limit}
        """,
        "cash landing per day",
        ("limit",),
    ),
    Intent.CASH_TOTAL: Template(
        Intent.CASH_TOTAL,
        """
        SELECT SUM(credited_paise) AS credited_paise,
               SUM(credit_count) AS credit_count,
               MIN(value_date) AS first_day, MAX(value_date) AS last_day
        FROM v_daily_cash
        """,
        "total cash landed across the period",
    ),
}


# --------------------------------------------------------------------------- #
# Deterministic intent recognition
# --------------------------------------------------------------------------- #
#: Patterns, most specific first. English and the Hinglish a merchant actually types.
_PATTERNS: Final[tuple[tuple[Intent, re.Pattern[str]], ...]] = (
    (Intent.SETTLEMENT_DETAIL, re.compile(r"\b(setl_[A-Za-z0-9_]+)", re.I)),
    (
        Intent.UNBALANCED_SETTLEMENTS,
        re.compile(r"\b(unbalanced|did ?n[o']?t balance|not balanc|unexplained|mismatch)", re.I),
    ),
    (
        Intent.TOTAL_FEES,
        re.compile(r"\b(fee|fees|mdr|gst|tds|commission|charges|kitna kata|deduct)", re.I),
    ),
    (
        Intent.EXCEPTIONS_BY_SEVERITY,
        re.compile(r"\b(urgent|severity|critical|high priority|most important)", re.I),
    ),
    (
        Intent.LARGEST_EXCEPTIONS,
        re.compile(r"\b(largest|biggest|worst|top \d+|most money|sabse bada)", re.I),
    ),
    (
        Intent.EXCEPTIONS_BY_TYPE,
        re.compile(r"\b(exception|break|unmatched|needs? review|problem|issue|kya galat)", re.I),
    ),
    (
        Intent.CASH_BY_DAY,
        re.compile(r"\b(per day|daily|each day|by day|day ?wise|roz)", re.I),
    ),
    (
        Intent.SETTLEMENT_ON_DATE,
        re.compile(r"\b(on|for)\s+(\d{4}-\d{2}-\d{2})", re.I),
    ),
    (
        Intent.CASH_TOTAL,
        re.compile(r"\b(total|how much|altogether|sum|overall|kitna aaya|received)", re.I),
    ),
    # Refunds and holds are reported as components of the fee breakdown, so a
    # question about them is answered from the same totals rather than refused.
    (Intent.TOTAL_FEES, re.compile(r"\b(refund|chargeback|dispute|reserve|withheld)", re.I)),
    # A specific settlement if one is named; otherwise the list. `_extract` supplies
    # no id for the plural form, so SETTLEMENT_DETAIL fails validation and falls
    # through to here rather than refusing an obvious question.
    (Intent.SETTLEMENT_DETAIL, re.compile(r"\bsetl_[A-Za-z0-9_]+", re.I)),
    (Intent.ALL_SETTLEMENTS, re.compile(r"\b(settlement|payout|settle)", re.I)),
)

_DATE_RE: Final = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_LIMIT_RE: Final = re.compile(r"\btop\s+(\d{1,3})\b", re.I)
_SETTLEMENT_RE: Final = re.compile(r"\b(setl_[A-Za-z0-9_]+)", re.I)

#: A question must be ABOUT something the three views actually model.
#:
#: Without this gate the patterns above are far too eager, because they key on
#: generic quantifiers. "How much did we lose to fraud?" matched CASH_TOTAL on the
#: words "how much" and confidently returned the period's cash total - answering a
#: question about fraud with a number about settlements. "Roughly what percentage of
#: merchants have this problem?" matched EXCEPTIONS_BY_TYPE on "problem" and answered
#: a question about the industry with this merchant's exception counts.
#:
#: Both are worse than refusing: the figure is real, the query ran, and the answer is
#: about something else entirely. So topic is checked first and independently - if the
#: question names nothing this data models, no pattern gets to fire.
#
# Note the trailing ``\w*`` on every noun rather than a closing ``\b``. An anchored
# alternation like ``\b(exception|...)\b`` does NOT match "exceptions" - the boundary
# after "exception" fails against the "s" - so the gate silently rejected half the
# questions it was meant to admit while still letting the out-of-scope ones through.
_IN_SCOPE: Final = re.compile(
    r"\b("
    r"settle\w*|payout\w*|payment\w*|invoice\w*|ledger\w*|book\w*|"
    r"exception\w*|break\w*|unmatched|unreconciled|mismatch\w*|residual\w*|balanc\w*|"
    r"fee\w*|mdr|gst|tds|commission\w*|charge\w*|deduct\w*|withheld|"
    r"cash|credit\w*|landed|receiv\w*|deposit\w*|"
    r"refund\w*|chargeback\w*|dispute\w*|reserve\w*|"
    r"reconcil\w*|kitna|kata|aaya|roz"
    r")",
    re.I,
)

#: Asks for something the reconciled ledger cannot supply, even on an in-scope topic.
#: A guess is still a guess when it is about settlements.
_SPECULATIVE: Final = re.compile(
    r"\b(estimate|guess|probably|roughly|approximate\w*|predict|will be|next "
    r"(quarter|month|year)|forecast|project(ion|ed)?|expect(ed)? to be)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """A question turned into a query we already wrote, or an honest refusal."""

    intent: Intent
    sql: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    template_description: str = ""
    matched_by: str = "pattern"
    """`pattern` when deterministic recognition sufficed; `llm` when it did not."""

    refusal: str = ""

    @property
    def supported(self) -> bool:
        return self.intent is not Intent.UNSUPPORTED and bool(self.sql)

    def canonical(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "sql": self.sql.strip(),
            "arguments": dict(sorted(self.arguments.items())),
            "matched_by": self.matched_by,
            "refusal": self.refusal,
        }


def _validate(intent: Intent, arguments: dict[str, Any]) -> dict[str, Any]:
    """Every parameter checked before it reaches a template.

    This is where a question stops being able to influence the query beyond choosing
    among the ones we wrote. A settlement id must look like a settlement id; a date
    must parse; a limit must be a small positive integer.
    """
    out: dict[str, Any] = {}
    template = TEMPLATES[intent]

    for name in template.parameters:
        raw = arguments.get(name)
        if name == "settlement_id":
            text = str(raw or "")
            if not re.fullmatch(r"setl_[A-Za-z0-9_]{1,60}", text):
                raise ValueError(f"not a settlement id: {text!r}")
            out[name] = text
        elif name == "as_of":
            text = str(raw or "")
            dt.date.fromisoformat(text)  # raises if malformed
            out[name] = text
        elif name == "limit":
            value = int(raw) if raw is not None else 10
            if not 1 <= value <= 200:
                raise ValueError(f"limit out of range: {value}")
            out[name] = value
        else:
            raise ValueError(f"unknown parameter {name}")
    return out


def compile_question(question: str, *, gateway: Any = None) -> CompiledQuery:
    """Turn a question into one of our queries, or refuse.

    Deterministic recognition first, because it is free, cannot be steered, and
    handles most of what a merchant actually asks. The model is consulted only when
    no pattern matches - and even then it is choosing from the same closed set.
    """
    text = (question or "").strip()
    if not text:
        return CompiledQuery(Intent.UNSUPPORTED, refusal="empty question")

    # Topic gate, before any pattern gets a chance. A question that names nothing
    # these three views model cannot be answered from them, however many generic
    # quantifiers it contains.
    if not _IN_SCOPE.search(text):
        return CompiledQuery(
            Intent.UNSUPPORTED,
            refusal=(
                "the question does not mention anything the reconciled data models - "
                "settlements, exceptions, fees, refunds, holds or cash landing. "
                "Answering it would mean returning a real number about something else."
            ),
        )

    if _SPECULATIVE.search(text):
        return CompiledQuery(
            Intent.UNSUPPORTED,
            refusal=(
                "the question asks for an estimate or a projection. The reconciled "
                "ledger holds what happened, not what might. For forward-looking cash, "
                "use the forecast, which ships its own measured interval."
            ),
        )

    for intent, pattern in _PATTERNS:
        if not pattern.search(text):
            continue
        try:
            arguments = _validate(intent, _extract(intent, text))
        except (ValueError, TypeError):
            continue
        return CompiledQuery(
            intent=intent,
            sql=TEMPLATES[intent].render(arguments),
            arguments=arguments,
            template_description=TEMPLATES[intent].description,
            matched_by="pattern",
        )

    if gateway is not None:
        chosen = _ask_model(text, gateway)
        if chosen is not None:
            return chosen

    return CompiledQuery(
        Intent.UNSUPPORTED,
        refusal=(
            "That is not something the reconciled data can answer. Triveni answers "
            "from three documented views - settlements, exceptions and daily cash - "
            "and will not invent a query outside them."
        ),
    )


def _extract(intent: Intent, text: str) -> dict[str, Any]:
    """Pull the parameters a template needs straight out of the question."""
    arguments: dict[str, Any] = {}
    if "settlement_id" in TEMPLATES[intent].parameters:
        match = _SETTLEMENT_RE.search(text)
        arguments["settlement_id"] = match.group(1) if match else ""
    if "as_of" in TEMPLATES[intent].parameters:
        match = _DATE_RE.search(text)
        arguments["as_of"] = match.group(1) if match else ""
    if "limit" in TEMPLATES[intent].parameters:
        match = _LIMIT_RE.search(text)
        arguments["limit"] = int(match.group(1)) if match else 10
    return arguments


def _ask_model(question: str, gateway: Any) -> CompiledQuery | None:
    """Let the model pick a template when patterns could not.

    It returns an intent name and arguments - never SQL. An intent it invents is not
    in the enum and is refused; arguments it invents go through the same validation
    as any other. The blast radius of a fully adversarial response is "runs a query
    we already wrote against the wrong parameters", and validation shrinks even that.
    """
    listing = "\n".join(
        f"  {intent.value}: {template.description}" for intent, template in TEMPLATES.items()
    )
    outcome = gateway.call(
        "compile_question",
        {"catalogue": listing},
        untrusted={"question": question},
    )
    if outcome.abstained or not outcome.data:
        return None

    try:
        intent = Intent(outcome.data.get("intent", "unsupported"))
    except ValueError:
        # An intent outside the enum is the entire blast radius of an adversarial
        # model response, and it lands here as a refusal.
        return None
    if intent is Intent.UNSUPPORTED:
        return CompiledQuery(
            Intent.UNSUPPORTED,
            matched_by="llm",
            refusal="the model found no query in the catalogue that answers this",
        )

    try:
        arguments = _validate(intent, outcome.data)
    except (ValueError, TypeError) as exc:
        return CompiledQuery(
            Intent.UNSUPPORTED,
            matched_by="llm",
            refusal=f"the chosen query needs a parameter the question did not supply: {exc}",
        )

    return CompiledQuery(
        intent=intent,
        sql=TEMPLATES[intent].render(arguments),
        arguments=arguments,
        template_description=TEMPLATES[intent].description,
        matched_by="llm",
    )


def assert_reads_only_allowed_views(sql: str) -> None:
    """Belt to the templates' braces: nothing may reference a table we did not name.

    Templates are hand-written, so this can only fail if someone adds one carelessly -
    which is exactly when a check like this earns its place.
    """
    referenced = set(re.findall(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I))
    referenced |= set(re.findall(r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I))
    unknown = {name for name in referenced if name not in ALLOWED_VIEWS}
    if unknown:
        raise ValueError(f"query references non-view tables: {sorted(unknown)}")


def catalogue() -> list[dict[str, str]]:
    """What Triveni can answer, for the UI's empty state and the README."""
    return [
        {"intent": intent.value, "answers": template.description}
        for intent, template in TEMPLATES.items()
    ]


#: Exception types, re-exported so a caller can enumerate what may appear in answers.
KNOWN_EXCEPTION_TYPES: Final[frozenset[str]] = frozenset(t.value for t in ExceptionType)
