"""Reading the gateway's own JSON, offline, from recorded fixtures.

This is the ``TRIVENI_RAILS=offline`` implementation. It reads files shaped exactly
like Razorpay API responses - the ``entity``/``count``/``items`` envelope, amounts as
integer paise, ``pay_``/``rfnd_``/``setl_`` id prefixes, epoch ``created_at`` - and
turns them into :class:`~ingest.canonical.CanonicalTxn`.

The point of keeping the envelope shape rather than a convenient flat list is that
``ingest/adapters/razorpay_mcp.py`` returns the same structures from the live server,
so a contract test can assert both implementations satisfy the same model. Switching
rails must not be able to break the pipeline.

Nothing here reads the ground truth. A row's settlement group is something the matcher
has to work out, not something the ingest layer is told.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from core.clock import from_epoch
from core.money import Money
from ingest.canonical import (
    CanonicalTxn,
    CorruptRecord,
    Direction,
    IngestResult,
    PaymentMethod,
    SourceKind,
    TxnKind,
    build_txn,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_FIXTURES = ROOT / "data" / "seed"


def _load_envelope(path: Path) -> list[dict[str, Any]]:
    """Read a Razorpay-shaped collection. A missing file is an empty collection,
    not a crash: a merchant with no refunds that week is normal."""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise TypeError(f"{path.name}: 'items' is not a list")
    return items


def _method(raw: str) -> PaymentMethod:
    try:
        return PaymentMethod(raw)
    except ValueError:
        # An unrecognised method is not a parse failure - it just means we cannot
        # assume a fee rate for it, which the decomposition will notice.
        return PaymentMethod.UNKNOWN


def read_payments(directory: Path = DEFAULT_FIXTURES) -> IngestResult:
    rows: list[CanonicalTxn] = []
    corrupt: list[CorruptRecord] = []
    for index, item in enumerate(_load_envelope(directory / "gateway_payments.json")):
        try:
            captured = from_epoch(int(item["created_at"]))
            notes = item.get("notes") or {}
            rows.append(
                build_txn(
                    source=SourceKind.GATEWAY,
                    kind=TxnKind.PAYMENT,
                    external_id=str(item["id"]),
                    amount=Money(int(item["amount"]), str(item.get("currency", "INR"))),
                    direction=Direction.CREDIT,
                    occurred_at=captured,
                    method=_method(str(item.get("method", ""))),
                    counterparty=str(notes.get("counterparty", "")),
                    reference=str(item.get("order_id", "")),
                    parent_id=str(item.get("order_id", "")),
                    metadata={"invoice_no": notes.get("invoice_no", ""), "status": item.get("status", "")},
                    raw=item,
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            corrupt.append(CorruptRecord.from_error(SourceKind.GATEWAY, index, exc, item))
    return IngestResult(source=SourceKind.GATEWAY, rows=tuple(rows), corrupt=tuple(corrupt))


def read_refunds(directory: Path = DEFAULT_FIXTURES) -> IngestResult:
    """Refunds are debits. Signing them here means the netting arithmetic downstream
    is a plain sum rather than a special case."""
    rows: list[CanonicalTxn] = []
    corrupt: list[CorruptRecord] = []
    for index, item in enumerate(_load_envelope(directory / "gateway_refunds.json")):
        try:
            rows.append(
                build_txn(
                    source=SourceKind.GATEWAY,
                    kind=TxnKind.REFUND,
                    external_id=str(item["id"]),
                    amount=Money(int(item["amount"]), str(item.get("currency", "INR"))),
                    direction=Direction.DEBIT,
                    occurred_at=from_epoch(int(item["created_at"])),
                    reference=str(item.get("payment_id", "")),
                    parent_id=str(item.get("payment_id", "")),
                    raw_narration=str((item.get("notes") or {}).get("reason", "")),
                    metadata={"status": item.get("status", "")},
                    raw=item,
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            corrupt.append(CorruptRecord.from_error(SourceKind.GATEWAY, index, exc, item))
    return IngestResult(source=SourceKind.GATEWAY, rows=tuple(rows), corrupt=tuple(corrupt))


def read_settlements(directory: Path = DEFAULT_FIXTURES) -> IngestResult:
    """The gateway's own view of each payout.

    Useful as corroboration but deliberately *not* treated as the answer: the whole
    point of a three-way reconciliation is that the gateway's opinion of what it paid
    and the bank's record of what arrived are separate facts that have to agree.
    """
    rows: list[CanonicalTxn] = []
    corrupt: list[CorruptRecord] = []
    for index, item in enumerate(_load_envelope(directory / "gateway_settlements.json")):
        try:
            rows.append(
                build_txn(
                    source=SourceKind.GATEWAY,
                    kind=TxnKind.SETTLEMENT,
                    external_id=str(item["id"]),
                    amount=Money(int(item["amount"])),
                    direction=Direction.CREDIT,
                    occurred_at=from_epoch(int(item["created_at"])),
                    reference=str(item.get("utr", "")),
                    raw_reference=str(item.get("utr", "")),
                    metadata={
                        "fees_paise": int(item.get("fees", 0)),
                        "tax_paise": int(item.get("tax", 0)),
                        "status": item.get("status", ""),
                    },
                    raw=item,
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            corrupt.append(CorruptRecord.from_error(SourceKind.GATEWAY, index, exc, item))
    return IngestResult(source=SourceKind.GATEWAY, rows=tuple(rows), corrupt=tuple(corrupt))


def read_gateway(directory: Path = DEFAULT_FIXTURES) -> IngestResult:
    """Payments, refunds and settlements as one gateway-side result."""
    parts = [read_payments(directory), read_refunds(directory), read_settlements(directory)]
    return IngestResult(
        source=SourceKind.GATEWAY,
        rows=tuple(row for part in parts for row in part.rows),
        corrupt=tuple(record for part in parts for record in part.corrupt),
    )


def date_range(result: IngestResult) -> tuple[dt.date, dt.date] | None:
    if not result.rows:
        return None
    days = sorted(row.occurred_on for row in result.rows)
    return days[0], days[-1]
