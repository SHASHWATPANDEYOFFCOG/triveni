"""``CanonicalTxn`` - the one internal schema every source collapses into.

Three ledgers arrive in three shapes. The gateway sends JSON with paise integers and
epoch timestamps. The bank sends a CSV with a free-text narration column, amounts as
strings with commas, and a Cr/Dr marker. The merchant's own ledger sends invoice rows
with a counterparty name typed by a human. Nothing downstream should have to know any
of that.

So everything is normalised here, at the boundary, and exactly once:

* amounts become :class:`~core.money.Money` (integer paise);
* timestamps become timezone-aware IST;
* the free text is preserved *verbatim* in ``raw_narration`` alongside whatever was
  parsed out of it, because the LLM's extraction has to be checkable against the
  original and because the UI highlights the exact source span it came from;
* the untouched source row is kept in ``raw`` so a corrupt row can still be shown to
  a human as evidence rather than discarded.

The last point matters more than it looks. A reconciliation system that drops rows it
cannot parse silently loses money. A row that fails to canonicalise becomes a
``corrupt_row`` exception carrying its own raw payload, and the batch continues.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.clock import IST, ensure_aware
from core.errors import CorruptRow
from core.ids import txn_id
from core.money import Money, format_inr


class SourceKind(StrEnum):
    """Which of the three rivers this row came from."""

    GATEWAY = "gateway"
    """The payment gateway: payments, refunds, settlements, disputes."""

    BANK = "bank"
    """The bank statement: what actually landed in the account."""

    LEDGER = "ledger"
    """The merchant's own books: orders, invoices, expected receipts."""


class Direction(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class PaymentMethod(StrEnum):
    """Method matters because it determines the fee. UPI and RuPay debit carry zero
    MDR by regulation, which is why a fee model that assumes one blended rate cannot
    reconcile an Indian merchant's books."""

    UPI = "upi"
    RUPAY_DEBIT = "rupay_debit"
    CARD_DEBIT = "card_debit"
    CARD_CREDIT = "card_credit"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"
    INTERNATIONAL_CARD = "international_card"
    BANK_TRANSFER = "bank_transfer"
    UNKNOWN = "unknown"


#: Methods on which no merchant discount rate may be charged in India.
ZERO_MDR_METHODS: frozenset[PaymentMethod] = frozenset(
    {PaymentMethod.UPI, PaymentMethod.RUPAY_DEBIT}
)


class TxnKind(StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    SETTLEMENT = "settlement"
    CHARGEBACK = "chargeback"
    ADJUSTMENT = "adjustment"
    INVOICE = "invoice"
    FEE = "fee"


class CanonicalTxn(BaseModel):
    """One row, from any source, in the only shape the pipeline knows about."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    txn_id: str = Field(min_length=1)
    source: SourceKind
    kind: TxnKind
    external_id: str = Field(min_length=1, description="the id in the source system")

    amount: Money
    direction: Direction
    occurred_at: dt.datetime
    """Capture time for a payment, value date for a bank line, invoice date for a
    ledger row. Always timezone-aware."""

    settled_on: dt.date | None = None
    """When the money is expected or observed to land. None until known."""

    method: PaymentMethod = PaymentMethod.UNKNOWN
    counterparty: str = ""
    """Normalised at Stage 0; ``raw_counterparty`` keeps what the source said."""

    raw_counterparty: str = ""
    reference: str = ""
    """Normalised UTR / RRN / order id - the strongest deterministic key we have."""

    raw_reference: str = ""
    raw_narration: str = ""
    """Verbatim source text. The only field the LLM is allowed to read."""

    parent_id: str = ""
    """Links a refund to its payment, or a payment to its settlement batch."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    """The untouched source row, retained so a corrupt row is still evidence."""

    @field_validator("occurred_at")
    @classmethod
    def _must_be_aware(cls, value: dt.datetime) -> dt.datetime:
        return ensure_aware(value).astimezone(IST)

    @model_validator(mode="after")
    def _defaults(self) -> Self:
        # Keeping raw_* populated means a normaliser bug is always diagnosable: you
        # can see both what arrived and what we made of it.
        if not self.raw_counterparty:
            object.__setattr__(self, "raw_counterparty", self.counterparty)
        if not self.raw_reference:
            object.__setattr__(self, "raw_reference", self.reference)
        return self

    # --- derived -----------------------------------------------------------
    @property
    def signed_amount(self) -> Money:
        """Amount with direction applied, so sums work without branching."""
        return self.amount if self.direction is Direction.CREDIT else -self.amount

    @property
    def occurred_on(self) -> dt.date:
        return self.occurred_at.astimezone(IST).date()

    @property
    def zero_mdr(self) -> bool:
        return self.method in ZERO_MDR_METHODS

    def canonical(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "source": self.source.value,
            "kind": self.kind.value,
            "external_id": self.external_id,
            "amount": {"paise": self.amount.paise, "currency": self.amount.currency},
            "direction": self.direction.value,
            "occurred_at": self.occurred_at,
            "settled_on": self.settled_on,
            "method": self.method.value,
            "counterparty": self.counterparty,
            "reference": self.reference,
            "raw_narration": self.raw_narration,
            "parent_id": self.parent_id,
        }

    def render(self) -> str:
        return (
            f"{self.source.value:8} {self.kind.value:11} {self.external_id:22} "
            f"{format_inr(self.signed_amount):>15} {self.occurred_on} "
            f"{self.counterparty[:24]:24} {self.reference}"
        )


def build_txn(
    *,
    source: SourceKind,
    kind: TxnKind,
    external_id: str,
    amount: Money,
    direction: Direction,
    occurred_at: dt.datetime,
    **rest: Any,
) -> CanonicalTxn:
    """Construct with a deterministic, source-namespaced id.

    Namespacing by source matters: a gateway payment id and a bank reference can
    collide as strings while meaning entirely different things.
    """
    return CanonicalTxn(
        txn_id=txn_id(source.value, external_id),
        source=source,
        kind=kind,
        external_id=external_id,
        amount=amount,
        direction=direction,
        occurred_at=occurred_at,
        **rest,
    )


class CorruptRecord(BaseModel):
    """A row that could not be canonicalised, kept rather than dropped.

    Becomes a ``corrupt_row`` exception with the raw payload attached as evidence.
    Dropping it would be the single most dangerous thing an ingest layer can do:
    silently losing money and reporting a clean close.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: SourceKind
    row_number: int
    problem: str
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_error(
        cls, source: SourceKind, row_number: int, error: Exception, raw: dict[str, Any]
    ) -> CorruptRecord:
        return cls(source=source, row_number=row_number, problem=str(error), raw=raw)

    def as_error(self) -> CorruptRow:
        return CorruptRow(self.problem, source=self.source.value, row=self.row_number)


class IngestResult(BaseModel):
    """What one source yielded: the rows that parsed, and the ones that did not."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    source: SourceKind
    rows: tuple[CanonicalTxn, ...] = ()
    corrupt: tuple[CorruptRecord, ...] = ()

    @property
    def total_seen(self) -> int:
        return len(self.rows) + len(self.corrupt)

    @property
    def parse_rate(self) -> Decimal:
        """Share of source rows that canonicalised. Decimal, not float, for the same
        reason as everything else here: every ratio Triveni reports ends up in a
        metric, an audit record or the UI, and one spelling per value is worth more
        than the convenience."""
        if not self.total_seen:
            return Decimal(1)
        return Decimal(len(self.rows)) / Decimal(self.total_seen)

    def total_amount(self) -> Money:
        total = Money.zero()
        for row in self.rows:
            total = total + row.signed_amount
        return total
