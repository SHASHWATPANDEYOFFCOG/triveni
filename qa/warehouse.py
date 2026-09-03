"""Loading a reconciliation result into DuckDB, behind three documented views.

The question-answering layer never touches a Python object. It queries SQL, over a
surface defined in ``qa/semantic_view.sql``, because that is the only way to make the
grounding claim checkable: if every number in an answer has to appear in a result set,
there has to *be* a result set.

Everything loaded here is already reconciled. Nothing is computed at query time from
anything a model said.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb

from core.clock import INDIAN_BANK_CALENDAR
from ingest.canonical import Direction, SourceKind

ROOT: Final = Path(__file__).resolve().parent.parent
VIEW_SQL: Final = ROOT / "qa" / "semantic_view.sql"

#: The only tables the views are built on, and therefore the only tables that exist.
_BASE_TABLES: Final[tuple[str, ...]] = ("settlements_raw", "exceptions_raw", "daily_cash_raw")

#: Views the compiler is allowed to name. Anything else is refused before execution.
ALLOWED_VIEWS: Final[frozenset[str]] = frozenset(
    {"v_settlements", "v_exceptions", "v_daily_cash"}
)


@dataclass(slots=True)
class Warehouse:
    """An in-memory DuckDB holding one run's reconciled facts."""

    connection: Any
    row_counts: dict[str, int]

    def query(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Execute and return (column names, rows). Read-only by construction:
        the connection is opened over an in-memory database built from the run, so
        the worst a malformed query can do is fail."""
        cursor = self.connection.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return columns, cursor.fetchall()

    def schema(self) -> str:
        """The documented schema, verbatim from the .sql file.

        Handed to the model as the *only* description of what exists. A model that
        cannot see a column cannot invent a query against it.
        """
        return VIEW_SQL.read_text(encoding="utf-8")

    def close(self) -> None:
        self.connection.close()


def build(result: Any) -> Warehouse:
    """Load a ``ReconResult`` into DuckDB behind the semantic views."""
    connection = duckdb.connect(":memory:")

    connection.execute(
        """
        CREATE TABLE settlements_raw (
            settlement_id VARCHAR, settled_on DATE,
            gross_paise BIGINT, net_expected_paise BIGINT, net_received_paise BIGINT,
            residual_paise BIGINT, balanced BOOLEAN, member_count INTEGER,
            mdr_paise BIGINT, gst_paise BIGINT, tds_paise BIGINT,
            refunds_paise BIGINT, chargeback_paise BIGINT, reserve_paise BIGINT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE exceptions_raw (
            exception_id VARCHAR, exception_type VARCHAR, severity VARCHAR,
            amount_paise BIGINT, as_of DATE, reason VARCHAR,
            evidence_count INTEGER, stage VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE daily_cash_raw (
            value_date DATE, credited_paise BIGINT,
            credit_count INTEGER, is_business_day BOOLEAN
        )
        """
    )

    # --- settlements -------------------------------------------------------
    def component(waterfall: Any, name: str) -> int:
        """Sum one named component. `Line.component` is already an ExceptionType
        *value* (a str), so the taxonomy and the waterfall share one vocabulary and
        no enum lookup is needed here."""
        return sum(abs(line.amount.paise) for line in waterfall.lines if line.component == name)

    # Members per settlement come from the match group that produced it - the
    # waterfall itself records the arithmetic, not the membership.
    members_by_bank_row: dict[str, int] = {}
    for match in result.matches:
        for bank_id in match.bank_ids:
            members_by_bank_row[bank_id] = len(match.gateway_ids) or len(match.ledger_ids)

    settlement_rows = []
    for bank_row_id, waterfall in sorted(
        result.waterfalls.items(), key=lambda item: item[1].settlement_id
    ):
        settlement_rows.append(
            (
                waterfall.settlement_id,
                waterfall.as_of,
                waterfall.gross.paise,
                waterfall.net_expected.paise,
                waterfall.net_received.paise,
                waterfall.residual.paise,
                waterfall.balanced,
                members_by_bank_row.get(bank_row_id, 0),
                component(waterfall, "fee_mdr"),
                component(waterfall, "gst_on_fee"),
                component(waterfall, "tds_194o"),
                component(waterfall, "refund_offset"),
                component(waterfall, "chargeback_hold"),
                component(waterfall, "rolling_reserve"),
            )
        )
    if settlement_rows:
        connection.executemany(
            "INSERT INTO settlements_raw VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", settlement_rows
        )

    # --- exceptions --------------------------------------------------------
    exception_rows = [
        (
            exception.exception_id,
            exception.exception_type.value,
            exception.severity.value,
            exception.amount.paise,
            exception.as_of,
            exception.reason,
            exception.evidence.count(),
            exception.evidence.stage,
        )
        for exception in sorted(result.exceptions, key=lambda e: e.exception_id)
    ]
    if exception_rows:
        connection.executemany(
            "INSERT INTO exceptions_raw VALUES (?,?,?,?,?,?,?,?)", exception_rows
        )

    # --- daily cash --------------------------------------------------------
    by_date: dict[dt.date, tuple[int, int]] = {}
    for row in result.rows.values():
        if row.source is not SourceKind.BANK or row.direction is not Direction.CREDIT:
            continue
        day = row.settled_on or row.occurred_on
        total, count = by_date.get(day, (0, 0))
        by_date[day] = (total + row.amount.paise, count + 1)

    cash_rows = [
        (day, total, count, INDIAN_BANK_CALENDAR.is_business_day(day))
        for day, (total, count) in sorted(by_date.items())
    ]
    if cash_rows:
        connection.executemany("INSERT INTO daily_cash_raw VALUES (?,?,?,?)", cash_rows)

    connection.execute(VIEW_SQL.read_text(encoding="utf-8"))

    return Warehouse(
        connection=connection,
        row_counts={
            "v_settlements": len(settlement_rows),
            "v_exceptions": len(exception_rows),
            "v_daily_cash": len(cash_rows),
        },
    )
