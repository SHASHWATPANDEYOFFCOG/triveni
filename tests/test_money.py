"""M1 gate for money: exactness is a property, not a spot-check.

The headline requirement is "a property test proves no float ever enters a money
path". That is enforced two ways here, because either alone would be weak:

* **statically** - `scripts.money_lint` walks the AST of every money-path module and
  fails on float literals, `float()` calls and non-Decimal true division. A test
  below feeds it a deliberately broken module to prove the lint itself is not vacuous.
* **dynamically** - Hypothesis generates arbitrary floats and arbitrary amounts and
  asserts every constructor and operator refuses the former and stays exact on the
  latter.
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from core.errors import CurrencyMismatch, FloatInMoneyPath, NegativeAllocation
from core.money import (
    Money,
    Rate,
    allocate,
    format_inr,
    format_inr_compact,
    group_indian,
    split_evenly,
    sum_money,
)

# Amounts up to ~10 crore, positive and negative - the realistic daily range for an
# SMB settlement, plus the sign cases that a refund-heavy day produces.
paise = st.integers(min_value=-10_00_00_00_000, max_value=10_00_00_00_000)
positive_paise = st.integers(min_value=0, max_value=10_00_00_00_000)
weights = st.lists(st.integers(min_value=0, max_value=1000), min_size=1, max_size=25)

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# No float, ever - the dynamic half of the proof
# --------------------------------------------------------------------------- #
@given(st.floats(allow_nan=True, allow_infinity=True))
@settings(max_examples=200)
def test_no_float_can_construct_money(value: float) -> None:
    """Every constructor refuses every float, including 0.0, nan and inf."""
    with pytest.raises(FloatInMoneyPath):
        Money.from_paise(value)  # type: ignore[arg-type]
    with pytest.raises(FloatInMoneyPath):
        Money.from_rupees(value)  # type: ignore[arg-type]
    with pytest.raises(FloatInMoneyPath):
        Money(value)  # type: ignore[arg-type]


@given(st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6))
def test_no_float_can_scale_money(value: float) -> None:
    """A float cannot get in through multiplication or through a Rate either."""
    with pytest.raises(FloatInMoneyPath):
        Money(100) * value  # type: ignore[operator]
    with pytest.raises(FloatInMoneyPath):
        Rate.from_percent(value)  # type: ignore[arg-type]
    with pytest.raises(FloatInMoneyPath):
        Rate.from_bps(value)  # type: ignore[arg-type]


@given(paise)
def test_money_refuses_to_become_a_float(value: int) -> None:
    """The exit is closed as well as the entrance: `float(money)` raises."""
    money = Money(value)
    with pytest.raises(FloatInMoneyPath):
        float(money)
    with pytest.raises(FloatInMoneyPath):
        money / 2  # type: ignore[operator]
    with pytest.raises(FloatInMoneyPath):
        money // 2  # type: ignore[operator]


def test_bool_is_not_an_amount() -> None:
    """bool subclasses int, so it would sail through a naive isinstance check."""
    with pytest.raises(FloatInMoneyPath):
        Money.from_paise(True)  # type: ignore[arg-type]


@given(st.decimals(min_value=Decimal("-1e6"), max_value=Decimal("1e6"), places=3))
def test_sub_paise_precision_is_rejected_not_rounded(value: Decimal) -> None:
    """A source file with three decimal places is a source file we misunderstand."""
    assume(value * 100 != (value * 100).to_integral_value())
    with pytest.raises(FloatInMoneyPath):
        Money.from_rupees(value)


# --------------------------------------------------------------------------- #
# The static half: the lint, and proof the lint has teeth
# --------------------------------------------------------------------------- #
def test_money_lint_passes_on_the_real_money_path() -> None:
    from scripts.money_lint import check_all

    findings, _suppressions, checked = check_all()
    assert checked, "money-lint checked no modules - the module list is wrong"
    assert findings == [], "floats found on the money path:\n" + "\n".join(map(str, findings))


def test_money_lint_actually_catches_violations(tmp_path) -> None:
    """A lint that never fails proves nothing. Feed it each violation shape."""
    from scripts.money_lint import check_file

    offender = tmp_path / "core"
    offender.mkdir()
    module = offender / "money.py"
    module.write_text(
        "def fee(amount):\n"
        "    rate = 0.019\n"
        "    return float(amount) * rate / 3\n",
        encoding="utf-8",
    )
    import scripts.money_lint as lint

    original_root = lint.ROOT
    try:
        lint.ROOT = tmp_path
        findings, _ = check_file(module)
    finally:
        lint.ROOT = original_root
    rules = {f.rule for f in findings}
    assert rules == {"float-literal", "float-call", "true-division"}, rules


def test_money_lint_suppression_requires_a_reason(tmp_path) -> None:
    import scripts.money_lint as lint
    from scripts.money_lint import check_file

    module = tmp_path / "m.py"
    module.write_text(
        "a = 0.5  # money-lint: allow-float display only, never stored\n"
        "b = 0.25  # money-lint: allow-float\n",
        encoding="utf-8",
    )
    original_root = lint.ROOT
    try:
        lint.ROOT = tmp_path
        findings, suppressions = check_file(module)
    finally:
        lint.ROOT = original_root
    assert len(suppressions) == 1 and "display only" in suppressions[0].reason
    assert [f.rule for f in findings] == ["bare-suppression"]


# --------------------------------------------------------------------------- #
# Exact arithmetic
# --------------------------------------------------------------------------- #
@given(paise, paise)
def test_addition_is_exact_and_reversible(a: int, b: int) -> None:
    x, y = Money(a), Money(b)
    assert (x + y).paise == a + b
    assert (x + y - y) == x


@given(paise, paise, paise)
def test_addition_is_associative(a: int, b: int, c: int) -> None:
    x, y, z = Money(a), Money(b), Money(c)
    assert (x + y) + z == x + (y + z)


@given(st.lists(paise, max_size=50))
def test_sum_money_matches_integer_sum_and_returns_money_on_empty(values: list[int]) -> None:
    total = sum_money(Money(v) for v in values)
    assert isinstance(total, Money)
    assert total.paise == sum(values)


def test_cross_currency_arithmetic_is_refused() -> None:
    with pytest.raises(CurrencyMismatch):
        Money(1, "INR") + Money(1, "USD")
    with pytest.raises(CurrencyMismatch):
        Money(1, "INR") < Money(1, "USD")


# --------------------------------------------------------------------------- #
# Allocation - the conservation property
# --------------------------------------------------------------------------- #
@given(paise, weights)
def test_allocation_conserves_every_paise(amount: int, ws: list[int]) -> None:
    """The invariant that makes splitting a settlement safe: nothing evaporates."""
    assume(sum(ws) > 0)
    parts = allocate(Money(amount), ws)
    assert sum(p.paise for p in parts) == amount
    assert len(parts) == len(ws)


@given(positive_paise, weights)
def test_allocation_respects_weight_order(amount: int, ws: list[int]) -> None:
    """A bigger weight never receives less than a smaller one."""
    assume(sum(ws) > 0)
    parts = allocate(Money(amount), ws)
    for i in range(len(ws)):
        for j in range(len(ws)):
            if ws[i] > ws[j]:
                assert parts[i].paise >= parts[j].paise


@given(positive_paise, st.integers(min_value=1, max_value=40))
def test_even_split_differs_by_at_most_one_paise(amount: int, parts: int) -> None:
    shares = [m.paise for m in split_evenly(Money(amount), parts)]
    assert sum(shares) == amount
    assert max(shares) - min(shares) <= 1


@given(paise, weights)
def test_allocation_is_sign_symmetric(amount: int, ws: list[int]) -> None:
    """Reversing a settlement splits as the exact mirror of booking it."""
    assume(sum(ws) > 0)
    positive = allocate(Money(abs(amount)), ws)
    negative = allocate(Money(-abs(amount)), ws)
    assert [p.paise for p in positive] == [-n.paise for n in negative]


def test_allocation_is_the_textbook_case() -> None:
    assert [m.paise for m in allocate(Money(100), [1, 1, 1])] == [34, 33, 33]


def test_bad_weights_are_typed_errors() -> None:
    with pytest.raises(NegativeAllocation):
        allocate(Money(100), [])
    with pytest.raises(NegativeAllocation):
        allocate(Money(100), [0, 0])
    with pytest.raises(NegativeAllocation):
        allocate(Money(100), [1, -1])


# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #
@given(positive_paise)
def test_rate_application_is_deterministic_and_bounded(amount: int) -> None:
    rate = Rate.from_percent("1.90", label="MDR")
    fee = rate.of(Money(amount))
    assert fee == rate.of(Money(amount)), "same input must give the same fee, always"
    assert 0 <= fee.paise <= amount


def test_the_settlement_waterfall_rates_are_exact() -> None:
    """The worked example from the README, computed rather than copied."""
    gross = Money.from_rupees("1240000")
    mdr = Rate.from_percent("1.90", label="MDR").of(gross)
    gst = Rate.from_percent(18, label="GST on fee").of(mdr)
    tds = Rate.from_bps(10, label="TDS 194-O").of(gross)
    assert mdr == Money.from_rupees("23560")
    assert gst == Money.from_rupees("4240.80")
    assert tds == Money.from_rupees("1240")


def test_rate_percent_and_bps_agree() -> None:
    assert Rate.from_percent("1.90").value == Rate.from_bps(190).value


@given(st.integers(min_value=0, max_value=10_000))
def test_zero_percent_mdr_is_exactly_zero(amount: int) -> None:
    """UPI and RuPay debit carry zero MDR; the fee must be 0, not 0.0000001."""
    assert Rate.from_percent(0, label="UPI").of(Money(amount)).is_zero


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        ("1", "1"),
        ("12", "12"),
        ("123", "123"),
        ("1234", "1,234"),
        ("12345", "12,345"),
        ("123456", "1,23,456"),
        ("1240000", "12,40,000"),
        ("12345678", "1,23,45,678"),
        ("1234567890", "1,23,45,67,890"),
    ],
)
def test_indian_digit_grouping(digits: str, expected: str) -> None:
    assert group_indian(digits) == expected


@given(paise)
def test_format_then_parse_round_trips(amount: int) -> None:
    """Anything Triveni prints, Triveni can read back exactly."""
    money = Money(amount)
    assert Money.parse(format_inr(money)) == money


@pytest.mark.parametrize(
    ("text", "expected_paise"),
    [
        ("Rs. 12,40,000.00", 124_000_000),
        ("12,40,000", 124_000_000),
        ("(500.25)", -50_025),
        ("-500.25", -50_025),
        ("1,240.50 Dr", -124_050),
        ("1,240.50 Cr", 124_050),
        ("  900  ", 90_000),
    ],
)
def test_bank_csv_amount_shapes(text: str, expected_paise: int) -> None:
    assert Money.parse(text).paise == expected_paise


def test_unparseable_amounts_raise_rather_than_guess() -> None:
    for bad in ("", "   ", "abc", "12.3.4", "1,2,3.4.5"):
        with pytest.raises(FloatInMoneyPath):
            Money.parse(bad)


def test_compact_formatting_uses_lakh_and_crore() -> None:
    assert format_inr_compact(Money.from_rupees("1240000")) == "₹12.40 L"
    assert format_inr_compact(Money.from_rupees("12400000")) == "₹1.24 Cr"
    assert format_inr_compact(Money.from_rupees("900")) == "₹900"


def test_zero_and_sign_helpers() -> None:
    assert Money.zero().is_zero
    assert Money(5).sign == 1 and Money(-5).sign == -1 and Money(0).sign == 0
    assert abs(Money(-5)) == Money(5)
    assert (-Money(5)).paise == -5


def test_as_decimal_is_exact() -> None:
    """Display conversion is Decimal, so it stays exact - unlike float."""
    value = Money(124_000_050).as_decimal()
    assert value == Decimal("1240000.50")
    assert not math.isnan(float(value))  # Decimal -> float is fine for display only


def test_decimal_refuses_float_arithmetic() -> None:
    """The language guarantee money-lint's division rule rests on.

    `scripts/money_lint.py` permits a division when either operand is provably a
    Decimal, reasoning that Decimal refuses to mix with float. If a future Python
    ever relaxed that, the lint would be unsound - so the assumption is pinned here
    rather than left as a comment.
    """
    with pytest.raises(TypeError):
        Decimal(1) / 0.5
    with pytest.raises(TypeError):
        0.5 / Decimal(1)
    with pytest.raises(TypeError):
        Decimal(1) * 0.5
    with pytest.raises(TypeError):
        Decimal(1) + 0.5
    # int is fine, and stays exact.
    assert Decimal(1) / 4 == Decimal("0.25")


def test_money_lint_still_catches_a_bare_float_division() -> None:
    """The relaxed rule must not have opened a hole: no Decimal, still a finding."""
    from scripts.money_lint import check_file

    module = ROOT / "tests" / "_lint_probe.py"
    module.write_text("def f(total, n):\n    return total / n\n", encoding="utf-8")
    try:
        findings, _ = check_file(module)
    finally:
        module.unlink()
    assert [f.rule for f in findings] == ["true-division"]
