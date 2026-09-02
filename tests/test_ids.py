"""M1 gate for identity: one byte string per value, one id per content.

Canonical JSON is load-bearing twice over. It is the Merkle leaf encoding, so if two
machines could spell the same decision record differently the audit root would differ
with nothing having been tampered with. And it is the basis of every idempotency key,
so if it were unstable a re-run could double-post.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.clock import IST, parse_ist
from core.ids import (
    CanonicalisationError,
    IdKind,
    canonical_json,
    canonical_text,
    content_hash,
    digest,
    make_id,
    match_id,
    short,
    txn_id,
)
from core.money import Money

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.text(max_size=40),
)
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=6),
        st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=6),
    ),
    max_leaves=25,
)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
@given(json_values)
def test_canonical_json_is_stable_across_repeated_calls(value: object) -> None:
    assert canonical_json(value) == canonical_json(value)


@given(st.dictionaries(st.text(min_size=1, max_size=8), st.integers(), min_size=2, max_size=8))
def test_key_insertion_order_cannot_change_the_bytes(mapping: dict[str, int]) -> None:
    """The property that makes the Merkle root reproducible on another machine."""
    shuffled = dict(sorted(mapping.items(), reverse=True))
    assert canonical_json(mapping) == canonical_json(shuffled)


def test_sets_serialise_in_a_stable_order() -> None:
    """Set iteration order is not guaranteed; the canonical form must be."""
    assert canonical_json({"tags": {"b", "a", "c"}}) == canonical_json({"tags": {"c", "a", "b"}})
    assert canonical_text({"tags": {"b", "a"}}) == '{"tags":["a","b"]}'


def test_the_same_instant_in_two_zones_hashes_the_same() -> None:
    """A record written in IST and one written in UTC describe one instant, so they
    must produce one leaf."""
    ist = parse_ist("2026-03-31T18:30:00+05:30")
    utc = ist.astimezone(dt.UTC)
    assert canonical_json({"at": ist}) == canonical_json({"at": utc})


def test_decimal_spelling_is_normalised() -> None:
    """Decimal('1.50') and Decimal('1.5') are the same amount and one leaf."""
    assert canonical_json(Decimal("1.50")) == canonical_json(Decimal("1.5"))
    assert canonical_json(Decimal("0")) == canonical_json(Decimal("0.00"))


def test_canonical_json_output_has_no_insignificant_whitespace() -> None:
    assert canonical_text({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
@given(st.floats(allow_nan=False, allow_infinity=False))
def test_floats_have_no_canonical_form(value: float) -> None:
    """Same rule as core/money: a float cannot be spelled one way, so it cannot be
    hashed reproducibly, so it never reaches a leaf."""
    with pytest.raises(CanonicalisationError):
        canonical_json({"amount": value})


def test_naive_datetimes_have_no_canonical_form() -> None:
    with pytest.raises(CanonicalisationError):
        canonical_json(dt.datetime(2026, 3, 31, 18, 30))


def test_non_string_keys_are_refused() -> None:
    with pytest.raises(CanonicalisationError):
        canonical_json({1: "one"})


def test_unknown_types_are_refused_rather_than_stringified() -> None:
    class Mystery:
        pass

    with pytest.raises(CanonicalisationError):
        canonical_json(Mystery())


# --------------------------------------------------------------------------- #
# Domain values
# --------------------------------------------------------------------------- #
def test_money_canonicalises_through_its_dataclass_fields() -> None:
    assert canonical_text(Money(124_000_000)) == '{"currency":"INR","paise":124000000}'


def test_enums_canonicalise_to_their_values() -> None:
    assert canonical_text(IdKind.MATCH) == '"mtc"'


def test_bytes_canonicalise_as_hex() -> None:
    assert canonical_text(b"\x00\xff") == '"00ff"'


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #
@given(json_values)
def test_ids_are_pure_functions_of_content(value: object) -> None:
    assert make_id(IdKind.DECISION, value) == make_id(IdKind.DECISION, value)


def test_ids_are_typed_and_readable() -> None:
    identifier = make_id(IdKind.EXCEPTION, "payout-short")
    assert identifier.startswith("exc_")
    assert len(identifier) == len("exc_") + 32
    assert short(identifier) == identifier[: len("exc_") + 8]


def test_the_same_content_under_two_kinds_gives_two_ids() -> None:
    assert make_id(IdKind.MATCH, "x") != make_id(IdKind.EXCEPTION, "x")


def test_txn_ids_are_namespaced_by_source() -> None:
    """A gateway payment id and a bank reference can collide as strings while meaning
    entirely different things."""
    assert txn_id("gateway", "REF123") != txn_id("bank", "REF123")


def test_match_ids_ignore_discovery_order() -> None:
    """N invoices against 1 payout must hash identically however the solver
    enumerated them - otherwise idempotency depends on solver internals."""
    a = match_id(["inv_1", "inv_2", "inv_3"], ["pay_9"])
    b = match_id(["inv_3", "inv_1", "inv_2"], ["pay_9"])
    assert a == b
    assert match_id(["inv_1"], ["pay_9"]) != match_id(["pay_9"], ["inv_1"])


@given(json_values, json_values)
def test_different_content_gives_different_ids(a: object, b: object) -> None:
    if canonical_json(a) != canonical_json(b):
        assert make_id(IdKind.TXN, a) != make_id(IdKind.TXN, b)


def test_digest_and_content_hash_agree() -> None:
    payload = {"a": 1, "b": [2, 3]}
    assert digest(payload).hex() == content_hash(payload)
    assert len(digest(payload)) == 32


def test_ids_are_stable_against_a_recorded_golden() -> None:
    """A golden so that an accidental change to the canonical encoding - which would
    silently invalidate every historical audit proof - fails loudly here instead."""
    payload = {
        "as_of": dt.date(2026, 3, 31),
        "amount": Money(124_000_000),
        "at": dt.datetime(2026, 3, 31, 13, 0, tzinfo=dt.UTC),
        "kind": IdKind.MATCH,
        "rate": Decimal("0.019"),
    }
    assert canonical_text(payload) == (
        '{"amount":{"currency":"INR","paise":124000000},'
        '"as_of":"2026-03-31",'
        '"at":"2026-03-31T13:00:00.000000Z",'
        '"kind":"mtc",'
        '"rate":"19E-3"}'
    )
    assert content_hash(payload) == content_hash(
        {**payload, "at": dt.datetime(2026, 3, 31, 18, 30, tzinfo=IST)}
    )
