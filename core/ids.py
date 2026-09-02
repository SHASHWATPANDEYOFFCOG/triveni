"""Canonical serialisation and deterministic identifiers.

Two jobs, deliberately in one module because the second is built on the first.

**Canonical JSON.** Exactly one byte string per logical value. Keys sorted, no
insignificant whitespace, UTF-8, and - the part that matters - **floats are refused**.
A float has no single decimal spelling, so a log whose leaves contained floats could
not be re-derived byte-for-byte on another machine, and the Merkle root would differ
without anything having been tampered with. ``Decimal``, ``Money``, ``datetime`` and
``set`` all get one documented spelling here (RFC 8785 in spirit; we do not claim
full JCS conformance, only determinism and a stated encoding).

**Identifiers.** Every id is a pure function of the content it names, so re-running
the pipeline over the same input produces the same ids. That is what makes the whole
run idempotent: a re-close cannot double-post, because the second attempt derives the
identical idempotency key and the audit log already holds it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Final

from core.errors import TriveniError

#: Digest size for content-addressed ids. 16 bytes -> 32 hex chars: ample collision
#: resistance for a day's ledger while staying short enough to read in a UI.
_ID_DIGEST_BYTES: Final = 16

#: Merkle leaves and tree heads use the full 32 bytes.
HASH_BYTES: Final = 32


class IdKind(StrEnum):
    """Typed id prefixes. A bare hex string in a log tells you nothing; a prefixed
    one tells you what kind of thing went wrong without a database lookup."""

    TXN = "txn"
    RUN = "run"
    BATCH = "bat"
    MATCH = "mtc"
    GROUP = "grp"
    EXCEPTION = "exc"
    DECISION = "dec"
    EVIDENCE = "evd"
    PROMPT = "prm"
    APPROVAL = "apr"


class CanonicalisationError(TriveniError):
    """A value has no single, stable JSON spelling."""


def _canonicalise(value: Any) -> Any:
    """Reduce an arbitrary value to JSON-safe primitives with one spelling each."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        # Checked before int: bool is an int subclass and must stay true/false.
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalisationError(
            "floats have no canonical decimal spelling and are refused here",
            value=repr(value),
            hint="use int paise, a Decimal, or Money - see core/money.py",
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalisationError("non-finite Decimal", value=str(value))
        # Normalised sign-and-digits form, so Decimal('1.50') and Decimal('1.5')
        # cannot produce two different leaves for the same amount.
        normalised = value.normalize()
        sign, digits, exponent = normalised.as_tuple()
        if normalised == 0:
            return "0"
        return f"{'-' if sign else ''}{''.join(map(str, digits))}E{exponent}"
    if isinstance(value, dt.datetime):
        return _canonicalise_datetime(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Enum):
        return _canonicalise(value.value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            if not isinstance(key, str):
                raise CanonicalisationError("object keys must be strings", key=repr(key))
            out[key] = _canonicalise(sub)
        return {k: out[k] for k in sorted(out)}
    if isinstance(value, (set, frozenset)):
        # Sets have no order, so they are serialised as a sorted list of their
        # canonical forms - stable regardless of insertion order or PYTHONHASHSEED.
        return sorted((_canonicalise(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [_canonicalise(v) for v in value]
    # Value objects (Money, pydantic models, dataclasses) opt in by exposing one of
    # these, rather than this module having to import every type in the system.
    for attr in ("canonical", "model_dump", "_asdict"):
        method = getattr(value, attr, None)
        if callable(method):
            return _canonicalise(method())
    if hasattr(value, "__dataclass_fields__"):
        fields = sorted(value.__dataclass_fields__)
        return {f: _canonicalise(getattr(value, f)) for f in fields}
    raise CanonicalisationError(
        "no canonical form for this type",
        type=type(value).__name__,
        hint="give it a .canonical() method or convert it at the boundary",
    )


def _canonicalise_datetime(value: dt.datetime) -> str:
    """One spelling per instant: UTC, microsecond precision, trailing ``Z``.

    Storing local offsets would mean the same instant hashed two ways depending on
    which machine wrote the record.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise CanonicalisationError(
            "naive datetime has no canonical form", value=value.isoformat()
        )
    utc = value.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def canonical_json(value: Any) -> bytes:
    """The one byte string for this value. Merkle leaves are exactly this output."""
    return json.dumps(
        _canonicalise(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    """``canonical_json`` as text, for logs and UI display."""
    return canonical_json(value).decode("utf-8")


def digest(value: Any, *, size: int = HASH_BYTES) -> bytes:
    """BLAKE2b digest of the canonical form.

    BLAKE2b rather than SHA-256 for the *content* hashes because it is faster in pure
    Python and has a native parameterised digest size. The Merkle tree itself uses
    SHA-256, because RFC 6962 specifies SHA-256 and interoperability there is the
    whole point - see core/audit/merkle.py.
    """
    return hashlib.blake2b(canonical_json(value), digest_size=size).digest()


def content_hash(value: Any) -> str:
    """Hex digest of the canonical form."""
    return digest(value).hex()


def make_id(kind: IdKind, *parts: Any) -> str:
    """A deterministic, content-addressed identifier: ``mtc_1f3a...``.

    The same inputs always produce the same id, on any machine, in any order of
    execution. This is the idempotency key: re-running a close cannot double-post,
    because the second run derives an id the audit log has already seen.
    """
    body = digest({"kind": kind.value, "parts": list(parts)}, size=_ID_DIGEST_BYTES).hex()
    return f"{kind.value}_{body}"


def txn_id(source: str, external_id: str) -> str:
    """Identity of an ingested row. Namespaced by source, because a gateway payment
    id and a bank reference can collide as strings and mean different things."""
    return make_id(IdKind.TXN, source, external_id)


def match_id(left_ids: list[str], right_ids: list[str]) -> str:
    """Identity of a proposed match group, invariant to the order the members were
    discovered in - so N invoices against 1 payout hash the same however the solver
    happened to enumerate them."""
    return make_id(IdKind.MATCH, sorted(left_ids), sorted(right_ids))


def run_id(as_of: dt.date, dataset: str, config_hash: str) -> str:
    """Identity of one reconciliation run: what day, what data, what settings."""
    return make_id(IdKind.RUN, as_of, dataset, config_hash)


def short(identifier: str, *, keep: int = 8) -> str:
    """``mtc_1f3a9b2c...`` -> ``mtc_1f3a9b2c`` for dense tables."""
    prefix, _, body = identifier.partition("_")
    return f"{prefix}_{body[:keep]}" if body else identifier[:keep]
