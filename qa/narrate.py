"""Narration, and the check that makes "no fabricated figures" a fact rather than a hope.

The model's job here is the smallest one in the system: turn a result set into a
sentence. It does not decide what to query, it does not compute anything, and it does
not get to introduce a number.

That last part is *enforced*, not requested. After the model answers,
:func:`verify_grounding` extracts every numeral from its text and checks each one
against the values that came back from SQL. A numeral with no source in the result set
means the model produced a figure from somewhere other than the data, and the answer is
**discarded**. The user gets the raw table and an explicit abstention instead.

This is deliberately a blunt instrument. It will occasionally reject an answer that was
fine - a rounded restatement, an ordinal, a number in a settlement id. Every one of
those has a documented exemption below, and where an exemption cannot be justified the
answer is refused. The asymmetry is the point: showing a table when a sentence would
have done is a small cost, and a confidently wrong rupee figure in a finance tool is
not.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from core.money import Money, format_inr

#: Numerals in the model's answer. Handles 12,40,000.00 / 1240000 / 1.9% / -500.
_NUMERAL_RE: Final = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

#: Dates are lifted out BEFORE numerals are extracted, and checked separately.
#:
#: An ISO date is not three numbers. Left in the text, `2026-03-04` yields the
#: "numerals" 2026, -03 and -04, none of which appears in any result row, and a
#: perfectly good answer gets discarded for quoting a date the query returned. Dates
#: are still verified - just as dates, against the date values in the result set.
_DATE_RE: Final = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

#: Numbers that are never claims about the data and never need a source.
#:
#: Each of these is exempt for a stated reason, not because it was convenient. An
#: exemption without a reason is how a grounding check quietly stops working.
_ALWAYS_ALLOWED: Final[frozenset[str]] = frozenset(
    {
        "0",
        "1",
        "2",
        "100",  # percentage denominators and trivial counts
        "18",  # the statutory GST rate, fixed in law
        "194",  # section 194-O
    }
)

#: Tolerance when comparing a stated figure against a source value. Rupee-rounded
#: restatements of a paise figure are legitimate; a different number is not.
_RELATIVE_TOLERANCE: Final = Decimal("0.005")


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """Which numerals were sourced, which were not, and therefore whether to speak."""

    grounded: bool
    checked: tuple[str, ...] = ()
    ungrounded: tuple[str, ...] = ()
    sources: int = 0

    @property
    def reason(self) -> str:
        if self.grounded:
            return (
                f"every one of the {len(self.checked)} figure(s) in this answer appears "
                f"in the {self.sources} value(s) the query returned"
            )
        return (
            f"{len(self.ungrounded)} figure(s) in the answer do not appear in the query "
            f"result: {', '.join(self.ungrounded[:5])}"
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "grounded": self.grounded,
            "checked": list(self.checked),
            "ungrounded": list(self.ungrounded),
            "sources": self.sources,
        }


def _numeric_forms(value: Any) -> set[Decimal]:
    """Every form a source value may legitimately be restated in.

    A paise integer may be quoted as paise, as rupees, or as rupees rounded to the
    nearest whole - all three are the same fact, and rejecting a rupee restatement of
    a paise column would make the checker useless on the only output anyone wants.
    """
    forms: set[Decimal] = set()
    if isinstance(value, bool) or value is None:
        return forms
    if isinstance(value, int):
        forms.add(Decimal(value))
        forms.add(Decimal(value) / Decimal(100))  # paise -> rupees
        forms.add((Decimal(value) / Decimal(100)).quantize(Decimal(1)))
    elif isinstance(value, (float, Decimal)):
        forms.add(Decimal(str(value)))
    elif isinstance(value, dt.date):
        forms.update({Decimal(value.year), Decimal(value.month), Decimal(value.day)})
    return {f for f in forms if f.is_finite()}


def verify_grounding(answer: str, columns: list[str], rows: list[tuple[Any, ...]]) -> GroundingResult:
    """Check every numeral in ``answer`` against the values SQL returned.

    Returns a result rather than raising, because an ungrounded answer is not an
    error - it is a signal to abstain and show the table, which is a perfectly good
    outcome and often a better one.
    """
    allowed: set[Decimal] = set()
    allowed_dates: set[str] = set()
    for row in rows:
        for value in row:
            allowed |= _numeric_forms(value)
            if isinstance(value, dt.date):
                allowed_dates.add(value.isoformat())
    # Counts of things are legitimately quotable: "3 settlements did not balance".
    allowed.add(Decimal(len(rows)))
    allowed.add(Decimal(len(columns)))

    checked: list[str] = []
    ungrounded: list[str] = []

    # Dates first, as dates, then removed so their digits are not re-read as figures.
    text = answer or ""
    for stated_date in _DATE_RE.findall(text):
        checked.append(stated_date)
        if stated_date not in allowed_dates:
            ungrounded.append(stated_date)
    text = _DATE_RE.sub(" ", text)

    for raw in _NUMERAL_RE.findall(text):
        cleaned = raw.replace(",", "")
        if cleaned in _ALWAYS_ALLOWED:
            continue
        try:
            value = Decimal(cleaned)
        except ArithmeticError:
            continue
        checked.append(raw)
        if not any(_matches(value, candidate) for candidate in allowed):
            ungrounded.append(raw)

    return GroundingResult(
        grounded=not ungrounded,
        checked=tuple(checked),
        ungrounded=tuple(ungrounded),
        sources=len(allowed),
    )


def _matches(stated: Decimal, source: Decimal) -> bool:
    """Equal, or within a rounding tolerance of a source value."""
    if stated == source:
        return True
    if source == 0:
        return stated == 0
    return abs(stated - source) / abs(source) <= _RELATIVE_TOLERANCE


# --------------------------------------------------------------------------- #
# The answer
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Answer:
    """What the user gets: a sentence, or an honest refusal plus the table."""

    question: str
    answered: bool
    text: str
    reason: str
    sql: str = ""
    intent: str = ""
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    grounding: GroundingResult | None = None
    matched_by: str = ""

    def render(self) -> str:
        lines: list[str] = [f"Q: {self.question}"]
        if self.answered:
            lines.append(f"A: {self.text}")
        else:
            lines.append(f"A: [abstained] {self.text}")
        lines.append(f"   why: {self.reason}")
        if self.rows:
            lines.append(f"   from {len(self.rows)} row(s) of {self.intent}:")
            lines.append("     " + " | ".join(self.columns))
            for row in self.rows[:5]:
                lines.append("     " + " | ".join(_display(v) for v in row))
            if len(self.rows) > 5:
                lines.append(f"     ... {len(self.rows) - 5} more")
        return "\n".join(lines)

    def canonical(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answered": self.answered,
            "text": self.text,
            "reason": self.reason,
            "intent": self.intent,
            "sql": self.sql.strip(),
            "matched_by": self.matched_by,
            "row_count": len(self.rows),
            "grounding": self.grounding.canonical() if self.grounding else None,
        }


def _display(value: Any) -> str:
    """Paise columns render as rupees; everything else as itself."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int) and abs(value) >= 100:
        return format_inr(Money(value))
    return str(value)


def describe_rows(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """A deterministic sentence built from the result set alone.

    Used when no model is available and as the floor an LLM narration has to beat.
    It cannot be ungrounded, because it is assembled from the values themselves.
    """
    if not rows:
        return "The query returned no rows."
    if len(rows) == 1 and len(columns) > 1:
        parts = [
            f"{column} {_display(value)}" for column, value in zip(columns, rows[0], strict=True)
        ]
        return "; ".join(parts) + "."
    return f"{len(rows)} row(s) returned, across {', '.join(columns)}."


def answer_question(
    question: str,
    warehouse: Any,
    *,
    gateway: Any = None,
) -> Answer:
    """Compile, execute, narrate, and refuse to speak if the narration is ungrounded."""
    from qa.compile import assert_reads_only_allowed_views, compile_question

    compiled = compile_question(question, gateway=gateway)
    if not compiled.supported:
        return Answer(
            question=question,
            answered=False,
            text=(
                "I can't answer that from the reconciled data. Here is what I can "
                "answer from: settlements and their waterfalls, exceptions and their "
                "types, and cash landing per day."
            ),
            reason=compiled.refusal or "no query in the catalogue answers this",
            intent=compiled.intent.value,
            matched_by=compiled.matched_by,
        )

    assert_reads_only_allowed_views(compiled.sql)
    columns, rows = warehouse.query(compiled.sql)

    if not rows:
        return Answer(
            question=question,
            answered=False,
            text="The query ran and returned nothing, so there is no figure to report.",
            reason="empty result set - an answer here would have to be invented",
            sql=compiled.sql,
            intent=compiled.intent.value,
            columns=tuple(columns),
            matched_by=compiled.matched_by,
        )

    deterministic = describe_rows(columns, rows)
    text, source = deterministic, "deterministic"

    if gateway is not None:
        narrated = _narrate(question, columns, rows, gateway)
        if narrated:
            text, source = narrated, "llm"

    grounding = verify_grounding(text, columns, rows)
    if not grounding.grounded:
        # The model introduced a figure that is not in the data. Discard the whole
        # answer and show the table - a sentence that is 90% right about money is
        # worse than no sentence.
        return Answer(
            question=question,
            answered=False,
            text=(
                "I can't answer that from the data - the narration contained "
                f"{len(grounding.ungrounded)} figure(s) I could not trace to the query "
                "result, so I have discarded it. Here is the table I would have used."
            ),
            reason=grounding.reason,
            sql=compiled.sql,
            intent=compiled.intent.value,
            columns=tuple(columns),
            rows=tuple(rows),
            grounding=grounding,
            matched_by=compiled.matched_by,
        )

    return Answer(
        question=question,
        answered=True,
        text=text,
        reason=f"{grounding.reason} (narrated by {source})",
        sql=compiled.sql,
        intent=compiled.intent.value,
        columns=tuple(columns),
        rows=tuple(rows),
        grounding=grounding,
        matched_by=compiled.matched_by,
    )


def _narrate(
    question: str, columns: list[str], rows: list[tuple[Any, ...]], gateway: Any
) -> str | None:
    """Ask the model for one sentence over the result set. Numbers are checked after.

    Both the question and the table go in as ``untrusted``: the question came from a
    user and the table, though we computed it, contains merchant-supplied narration
    text. Neither is a instruction to the model, and the gateway's delimiter wrapping
    is what says so.
    """
    table = " | ".join(columns) + "\n" + "\n".join(
        " | ".join(str(value) for value in row) for row in rows[:20]
    )
    outcome = gateway.call(
        "narrate_result",
        {},
        untrusted={"question": question, "table": table},
    )
    if outcome.abstained or not outcome.data:
        return None
    answer = str(outcome.data.get("answer", "")).strip()
    return answer or None
