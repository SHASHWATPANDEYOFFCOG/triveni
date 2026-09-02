"""Reading a bank statement CSV, and the merchant's own ledger.

Bank statements are a hostile format and this module is where that hostility is
absorbed. Amounts arrive as `"2,86,311.05"` with Indian digit grouping, sometimes with
a `Cr`/`Dr` marker, sometimes in parentheses for a negative. Dates arrive in whatever
the bank felt like. The narration is a single free-text column carrying the rail, the
UTR, the counterparty and occasionally nothing useful at all.

The rule that governs everything here: **a row that cannot be parsed is never
dropped.** It becomes a :class:`~ingest.canonical.CorruptRecord` carrying its raw
payload, which becomes a typed `corrupt_row` exception with that payload as evidence,
and the batch continues. Silently discarding an unparseable row is the single most
dangerous thing an ingest layer can do: it loses money and then reports a clean close.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from core.clock import IST
from core.errors import ClockError, MoneyError
from core.money import Money
from ingest.canonical import (
    CanonicalTxn,
    CorruptRecord,
    Direction,
    IngestResult,
    SourceKind,
    TxnKind,
    build_txn,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT / "data" / "seed"

#: Date formats seen in Indian bank exports, most specific first.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d/%m/%y",
    "%m/%d/%Y",
)


def parse_statement_date(text: str) -> dt.date:
    """Parse a statement date, trying the formats Indian banks actually emit.

    ``%m/%d/%Y`` is last on purpose: ``03/04/2026`` is the 3rd of April in India, and
    trying the American ordering first would silently mis-date a third of every
    statement - which then manufactures phantom `timing` exceptions.
    """
    raw = text.strip()
    if not raw:
        raise ClockError("empty date", value=text)
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ClockError("unrecognised date format", value=text)


def _row_amount(row: dict[str, str]) -> tuple[Money, Direction]:
    """Resolve the credit/debit pair into one signed amount.

    A row with both populated, or neither, is malformed - and saying so is better
    than guessing, because guessing here changes the books.
    """
    credit_text = (row.get("credit") or "").strip()
    debit_text = (row.get("debit") or "").strip()
    if credit_text and debit_text:
        raise MoneyError("row has both a credit and a debit", credit=credit_text, debit=debit_text)
    if not credit_text and not debit_text:
        raise MoneyError("row has neither a credit nor a debit")
    if credit_text:
        return Money.parse(credit_text), Direction.CREDIT
    return Money.parse(debit_text), Direction.DEBIT


def read_bank_statement(
    path: Path | None = None, *, directory: Path = DEFAULT_DIR
) -> IngestResult:
    """Read the bank statement. Unparseable rows survive as evidence."""
    path = path or directory / "bank_statement.csv"
    rows: list[CanonicalTxn] = []
    corrupt: list[CorruptRecord] = []
    if not path.exists():
        return IngestResult(source=SourceKind.BANK, rows=(), corrupt=())

    for index, raw in enumerate(_iter_csv(path)):
        try:
            amount, direction = _row_amount(raw)
            value_date = parse_statement_date(raw.get("value_date", ""))
            narration = (raw.get("narration") or "").strip()
            rows.append(
                build_txn(
                    source=SourceKind.BANK,
                    kind=TxnKind.SETTLEMENT,
                    external_id=(raw.get("row_id") or f"bank-{index}").strip(),
                    amount=amount,
                    direction=direction,
                    occurred_at=dt.datetime.combine(value_date, dt.time(0, 0), tzinfo=IST),
                    settled_on=value_date,
                    reference=(raw.get("reference") or "").strip(),
                    raw_reference=(raw.get("reference") or "").strip(),
                    raw_narration=narration,
                    metadata={"balance": (raw.get("balance") or "").strip()},
                    raw=dict(raw),
                )
            )
        except (MoneyError, ClockError, ValueError, TypeError) as exc:
            corrupt.append(CorruptRecord.from_error(SourceKind.BANK, index, exc, dict(raw)))
    return IngestResult(source=SourceKind.BANK, rows=tuple(rows), corrupt=tuple(corrupt))


def read_ledger(path: Path | None = None, *, directory: Path = DEFAULT_DIR) -> IngestResult:
    """Read the merchant's own invoice ledger.

    The counterparty here was typed by a person, which is why it disagrees with the
    bank's spelling of the same legal entity and why `recon/normalize.py` exists.
    """
    path = path or directory / "ledger_invoices.csv"
    rows: list[CanonicalTxn] = []
    corrupt: list[CorruptRecord] = []
    if not path.exists():
        return IngestResult(source=SourceKind.LEDGER, rows=(), corrupt=())

    for index, raw in enumerate(_iter_csv(path)):
        try:
            invoice_date = parse_statement_date(raw.get("invoice_date", ""))
            rows.append(
                build_txn(
                    source=SourceKind.LEDGER,
                    kind=TxnKind.INVOICE,
                    external_id=(raw.get("invoice_no") or f"ledger-{index}").strip(),
                    amount=Money.parse(raw.get("amount", "")),
                    direction=Direction.CREDIT,
                    occurred_at=dt.datetime.combine(invoice_date, dt.time(0, 0), tzinfo=IST),
                    counterparty=(raw.get("counterparty") or "").strip(),
                    raw_counterparty=(raw.get("counterparty") or "").strip(),
                    reference=(raw.get("order_id") or "").strip(),
                    parent_id=(raw.get("order_id") or "").strip(),
                    metadata={"status": (raw.get("status") or "").strip()},
                    raw=dict(raw),
                )
            )
        except (MoneyError, ClockError, ValueError, TypeError) as exc:
            corrupt.append(CorruptRecord.from_error(SourceKind.LEDGER, index, exc, dict(raw)))
    return IngestResult(source=SourceKind.LEDGER, rows=tuple(rows), corrupt=tuple(corrupt))


def _iter_csv(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)
