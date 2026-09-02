"""Money as integer paise. Never a float, anywhere, for any reason.

Binary floating point cannot represent 0.01 exactly. A reconciliation engine whose
arithmetic drifts by a paise does not reconcile - it produces plausible-looking books
that do not balance, which is worse than no books at all. So:

* every amount is an ``int`` count of paise inside a frozen ``Money`` value object;
* ``Money`` refuses to be constructed from a float and refuses ``float()`` on the way
  out, so the type itself blocks the leak rather than a code review having to;
* percentages are a ``Rate`` backed by ``Decimal`` with an explicit rounding mode,
  because "1.9% of 12,40,000" must be *one* answer, not one per machine;
* splitting money uses largest-remainder allocation, which is exact: the parts always
  sum back to the whole, to the paise.

Division is deliberately not implemented. ``Money / n`` is the single most common way
imprecision enters a ledger, so it raises with a pointer to :func:`allocate` instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from typing import Final

from core.errors import CurrencyMismatch, FloatInMoneyPath, NegativeAllocation

INR: Final = "INR"
PAISE_PER_RUPEE: Final = 100

RUPEE = "₹"

#: Rupee amounts stay exact well beyond any realistic settlement volume; 34 digits
#: covers ~10^30 rupees, so this guard is against logic errors, not against scale.
_DECIMAL_PRECISION: Final = 34


def _reject_float(value: object, *, where: str) -> None:
    """The single chokepoint that keeps floats out of every money path.

    Note ``bool`` is an ``int`` subclass in Python, so it is rejected explicitly too:
    ``Money.from_paise(True)`` meaning one paise is never what anyone intended.
    """
    if isinstance(value, float):
        raise FloatInMoneyPath(
            "a float was offered where exact integer paise are required",
            value=value,
            where=where,
            hint="pass an int of paise, a str like '1240.50', or a Decimal",
        )
    if isinstance(value, bool):
        raise FloatInMoneyPath("a bool was offered as a money amount", value=value, where=where)


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact amount, held as a signed integer number of paise (minor units)."""

    paise: int
    currency: str = INR

    def __post_init__(self) -> None:
        _reject_float(self.paise, where="Money(paise=...)")
        if not isinstance(self.paise, int):
            raise FloatInMoneyPath(
                "money must be an integer number of paise",
                value=self.paise,
                type=type(self.paise).__name__,
            )
        if len(self.currency) != 3 or not self.currency.isupper():
            raise CurrencyMismatch(
                "currency must be a 3-letter uppercase code", currency=self.currency
            )

    # --- constructors ------------------------------------------------------
    @classmethod
    def zero(cls, currency: str = INR) -> Money:
        return cls(0, currency)

    @classmethod
    def from_paise(cls, paise: int, currency: str = INR) -> Money:
        _reject_float(paise, where="Money.from_paise")
        return cls(int(paise), currency)

    @classmethod
    def from_rupees(cls, rupees: int | str | Decimal, currency: str = INR) -> Money:
        """Build from a rupee amount: ``int``, ``str`` (``'1240.50'``) or ``Decimal``.

        More than two decimal places raises rather than silently rounding, because
        sub-paise precision in a source file means the source is not what we think.
        """
        _reject_float(rupees, where="Money.from_rupees")
        with localcontext() as ctx:
            ctx.prec = _DECIMAL_PRECISION
            amount = rupees if isinstance(rupees, Decimal) else Decimal(rupees)
            scaled = amount * PAISE_PER_RUPEE
            if scaled != scaled.to_integral_value():
                raise FloatInMoneyPath(
                    "rupee amount has sub-paise precision",
                    value=str(rupees),
                    where="Money.from_rupees",
                )
            return cls(int(scaled), currency)

    @classmethod
    def parse(cls, text: str, currency: str = INR) -> Money:
        """Parse a human or CSV amount.

        Handles the shapes bank statements actually ship: ``'Rs. 12,40,000.00'``,
        ``'1240000'``, ``'(500.25)'`` (accounting negative), ``'-500.25'``, and a
        trailing ``Cr``/``Dr`` marker. Bank CSVs are a hostile format; this is the
        one door they come through.
        """
        raw = text.strip()
        if not raw:
            raise FloatInMoneyPath("empty amount string", value=text, where="Money.parse")
        negative = False
        if raw.startswith("(") and raw.endswith(")"):
            negative, raw = True, raw[1:-1].strip()
        lowered = raw.lower()
        for suffix, sign in (("cr", 1), ("dr", -1)):
            if lowered.endswith(suffix):
                raw = raw[: -len(suffix)].strip()
                if sign < 0:
                    negative = not negative
                break
        for token in (RUPEE, "INR", "Rs.", "Rs", ",", " ", " "):
            raw = raw.replace(token, "")
        raw = raw.strip()
        if raw.startswith("-"):
            negative, raw = not negative, raw[1:]
        elif raw.startswith("+"):
            raw = raw[1:]
        if not raw or not raw.replace(".", "", 1).isdigit():
            raise FloatInMoneyPath("unparseable amount", value=text, where="Money.parse")
        value = cls.from_rupees(raw, currency)
        return -value if negative else value

    # --- arithmetic --------------------------------------------------------
    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(
                "cannot combine two currencies without an explicit FX conversion",
                left=self.currency,
                right=other.currency,
            )

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.paise + other.paise, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.paise - other.paise, self.currency)

    def __mul__(self, factor: int) -> Money:
        """Multiplication by a whole count only - three refunds of one amount.

        Scaling by a percentage goes through :class:`Rate`, which is explicit about
        its rounding mode.
        """
        _reject_float(factor, where="Money.__mul__")
        if not isinstance(factor, int):
            raise FloatInMoneyPath("money may only be multiplied by an int count", value=factor)
        return Money(self.paise * factor, self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.paise, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.paise), self.currency)

    def __truediv__(self, other: object) -> Money:
        raise FloatInMoneyPath(
            "money division is disabled because it silently loses paise",
            where="Money.__truediv__",
            hint="use allocate(money, weights) for an exact split, or Rate(...).of(money)",
        )

    __rtruediv__ = __truediv__
    __floordiv__ = __truediv__

    def __float__(self) -> float:
        raise FloatInMoneyPath(
            "refusing to convert money to float",
            where="float(Money)",
            hint="use .paise for exact arithmetic or .as_decimal() for display",
        )

    # --- comparison --------------------------------------------------------
    def __lt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.paise < other.paise

    def __le__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.paise <= other.paise

    def __gt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.paise > other.paise

    def __ge__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.paise >= other.paise

    # --- inspection --------------------------------------------------------
    @property
    def is_zero(self) -> bool:
        return self.paise == 0

    @property
    def sign(self) -> int:
        return (self.paise > 0) - (self.paise < 0)

    def as_decimal(self) -> Decimal:
        """Exact rupee value as a ``Decimal``, for display and for feeding solvers
        that want a real number - never for storing back into the ledger."""
        return Decimal(self.paise) / Decimal(PAISE_PER_RUPEE)

    def __str__(self) -> str:
        return format_inr(self)

    def __repr__(self) -> str:
        return f"Money({self.paise}, {self.currency!r})  # {format_inr(self)}"


# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Rate:
    """A proportion held exactly as a ``Decimal``, with its rounding mode attached.

    MDR at 1.90%, GST at 18% *on the fee*, TDS at 0.1% under section 194-O: each is a
    rate applied to an exact amount, and the answer must not depend on the machine.
    The rounding mode is part of the value because the gateway's rounding and ours
    have to agree to the paise, or the settlement waterfall will not close.
    """

    value: Decimal
    rounding: str = ROUND_HALF_UP
    label: str = ""

    def __post_init__(self) -> None:
        _reject_float(self.value, where="Rate(value=...)")
        if not isinstance(self.value, Decimal):
            raise FloatInMoneyPath("Rate must hold a Decimal", value=self.value)

    @classmethod
    def from_percent(
        cls, percent: int | str | Decimal, *, label: str = "", rounding: str = ROUND_HALF_UP
    ) -> Rate:
        _reject_float(percent, where="Rate.from_percent")
        with localcontext() as ctx:
            ctx.prec = _DECIMAL_PRECISION
            return cls(Decimal(percent) / Decimal(100), rounding, label)

    @classmethod
    def from_bps(
        cls, bps: int | str | Decimal, *, label: str = "", rounding: str = ROUND_HALF_UP
    ) -> Rate:
        """Basis points - how payment fees are actually quoted. 190 bps = 1.90%."""
        _reject_float(bps, where="Rate.from_bps")
        with localcontext() as ctx:
            ctx.prec = _DECIMAL_PRECISION
            return cls(Decimal(bps) / Decimal(10_000), rounding, label)

    def of(self, amount: Money) -> Money:
        """Apply this rate to an amount, rounding exactly once, at the paise."""
        with localcontext() as ctx:
            ctx.prec = _DECIMAL_PRECISION
            exact = Decimal(amount.paise) * self.value
            rounded = exact.quantize(Decimal(1), rounding=self.rounding)
            return Money(int(rounded), amount.currency)

    def as_percent(self) -> Decimal:
        return self.value * 100

    def as_bps(self) -> Decimal:
        return self.value * 10_000

    def __str__(self) -> str:
        pct = self.as_percent().normalize()
        prefix = f"{self.label} " if self.label else ""
        return f"{prefix}{pct}%"


ZERO_RATE: Final = Rate(Decimal(0), label="zero")


# --------------------------------------------------------------------------- #
# Exact splitting
# --------------------------------------------------------------------------- #
def allocate(amount: Money, weights: Sequence[int]) -> list[Money]:
    """Split ``amount`` across ``weights`` so that the parts sum back exactly.

    Largest-remainder (Hamilton) allocation: floor every share, then hand the leftover
    paise out one at a time to the largest fractional remainders, breaking ties by
    index so the result is deterministic. ``allocate(Money(100), [1, 1, 1])`` gives
    34/33/33 - never 33/33/33 with a paisa quietly evaporated.
    """
    if not weights:
        raise NegativeAllocation("cannot allocate across zero parts", weights=list(weights))
    for w in weights:
        _reject_float(w, where="allocate(weights)")
        if w < 0:
            raise NegativeAllocation(
                "allocation weights must be non-negative", weights=list(weights)
            )
    total_weight = sum(weights)
    if total_weight == 0:
        raise NegativeAllocation("allocation weights sum to zero", weights=list(weights))

    # Work on the magnitude so a negative amount splits as the exact mirror image of
    # the positive one - an important symmetry when reversing a settlement.
    sign = -1 if amount.paise < 0 else 1
    total = abs(amount.paise)

    shares = [total * w // total_weight for w in weights]
    remainder = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-((total * weights[i]) % total_weight), i))
    for k in range(remainder):
        shares[order[k]] += 1

    return [Money(sign * s, amount.currency) for s in shares]


def split_evenly(amount: Money, parts: int) -> list[Money]:
    """Exact even split. ``split_evenly(Money(100), 3)`` -> 34, 33, 33."""
    if parts <= 0:
        raise NegativeAllocation("parts must be positive", parts=parts)
    return allocate(amount, [1] * parts)


def sum_money(amounts: Iterable[Money], currency: str = INR) -> Money:
    """Sum returning an explicit ``Money`` zero for an empty iterable.

    ``builtins.sum`` would hand back ``int`` 0, which then poisons the next currency
    check with a confusing error a long way from the cause.
    """
    total = Money.zero(currency)
    for amount in amounts:
        total = total + amount
    return total


# --------------------------------------------------------------------------- #
# Indian formatting
# --------------------------------------------------------------------------- #
def group_indian(digits: str) -> str:
    """Group digits the Indian way: last three, then pairs. 1240000 -> 12,40,000."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs: list[str] = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join([*pairs, tail])


def format_inr(amount: Money, *, paise: bool = True, symbol: bool = True) -> str:
    """``Money(124000000)`` -> ``'INR 12,40,000.00'`` with the rupee sign.

    Right-align these in the UI with ``font-variant-numeric: tabular-nums``; see
    ``web/styles/tokens.css``.
    """
    sign = "-" if amount.paise < 0 else ""
    magnitude = abs(amount.paise)
    rupees, minor = divmod(magnitude, PAISE_PER_RUPEE)
    body = group_indian(str(rupees))
    if paise:
        body = f"{body}.{minor:02d}"
    if not symbol:
        prefix = ""
    elif amount.currency == INR:
        prefix = RUPEE
    else:
        prefix = f"{amount.currency} "
    return f"{sign}{prefix}{body}"


def format_inr_compact(amount: Money) -> str:
    """``'12.40 L'`` / ``'1.24 Cr'`` for dense dashboard tiles. Display only."""
    magnitude = abs(amount.paise)
    sign = "-" if amount.paise < 0 else ""
    crore = 100_00_00_000  # one crore rupees, in paise
    lakh = 1_00_00_000  # one lakh rupees, in paise
    if magnitude >= crore:
        value = (Decimal(magnitude) / Decimal(crore)).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
        return f"{sign}{RUPEE}{value} Cr"
    if magnitude >= lakh:
        value = (Decimal(magnitude) / Decimal(lakh)).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
        return f"{sign}{RUPEE}{value} L"
    return format_inr(amount, paise=False)
