# ADR 0017 - The model never writes SQL, and never introduces a number

**Status:** accepted - 2026-09-03

**Context.** "Why is today's payout Rs 4,120 short?" is the question a merchant
actually asks, and the obvious implementation - hand a model the schema, ask for a
query, let it narrate the result - is wrong twice over for a finance tool. A free-form
query can join across a boundary that makes no sense or aggregate a column whose
meaning it guessed. And a model narrating a result set will, given the chance, produce
a figure that is not in it.

**Decision one: template selection, not SQL generation.**

    question -> intent + parameters -> a query we wrote -> execute -> rows

Ten hand-written templates over three documented views, each readable in ten seconds
and each asserted by a test to touch nothing else. The model's entire job is to pick
one and extract its arguments, and the arguments are validated - a settlement id must
match its shape, a date must parse, a limit must be 1-200. An intent outside the enum
is refused. The blast radius of a fully adversarial model response is therefore "runs
one of our own queries with the wrong parameters", and validation shrinks even that.

Pattern matching runs first, because most of what a merchant asks is recognisable
deterministically, costs nothing, and cannot be steered.

**Decision two: every numeral is checked against the rows.**

After narration, every number in the answer is extracted and matched against the values
SQL returned - accepting paise, the rupee restatement, and a rounding tolerance, because
rejecting "Rs 5,000" for a column holding 500000 would make the checker useless on the
only output anyone wants. A numeral with no source means the model produced a figure
from somewhere other than the data, and **the whole answer is discarded**. The user gets
an explicit abstention and the raw table.

The asymmetry is deliberate: showing a table where a sentence would have done is a small
cost; a confidently wrong rupee figure in a finance tool is not.

## The three bugs, all of the same family

**A generic quantifier is not an intent.** "How much did we lose to fraud?" matched
`CASH_TOTAL` on the words "how much" and returned the period's real cash total. "Roughly
what percentage of merchants have this problem?" matched `EXCEPTIONS_BY_TYPE` on
"problem" and answered with this merchant's exception counts. Both are **worse than a
refusal**: the number is correct, the query ran, and the answer is about something else.
Fixed with a topic gate that runs before any pattern - a question must name something
the three views actually model, or nothing fires.

**An anchored alternation rejects its own plurals.** The first topic gate was
`\b(exception|...)\b`, which does not match "exceptions" - the boundary fails against
the "s". It silently blocked half the questions it was meant to admit while still
letting the out-of-scope ones through, which is the worst of both.

**An ISO date is not three numbers.** Left in the text, `2026-03-04` yields the
"numerals" 2026, -03 and -04, none of which appears in any row - so a perfectly good
answer was discarded for quoting a date the query itself had returned. Dates are now
lifted out first and verified *as dates*, then removed before numeral extraction.

## Measured

`scripts/qa_adversarial.py`: **12 adversarial questions, all abstain; 4 answerable
questions, all answered with every figure traced to a row.** The answerable half is not
decoration - a system that abstains on everything is not grounded, it is mute, and the
suite must not be passable by refusing.

Every exemption in the grounding checker carries a written reason (18 for the statutory
GST rate, 194 for the section, row counts because "3 settlements did not balance" is a
claim about the result set itself). An exemption without a reason is how a check like
this quietly stops working.
