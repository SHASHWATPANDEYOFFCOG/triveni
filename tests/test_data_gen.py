"""M5 gate: the dataset is realistic, labelled, deterministic and does not cheat.

The Definition of Done is 5,000 rows with all sixteen exception types injected, a
~300-row committed seed, and a documented distribution. But the tests that matter
most here are the integrity ones. A benchmark whose labels are wrong, or whose source
files contain a field that gives away the answer, produces metrics that look fine and
mean nothing - and every number downstream inherits the lie.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from core.money import Money, Rate
from data.gen import (
    FULL_SPEC,
    MDR_BPS,
    SEED_DIR,
    SEED_SPEC,
    DatasetSpec,
    coverage,
    fee_components,
    generate,
)
from ingest.canonical import ZERO_MDR_METHODS, PaymentMethod
from recon.exceptions import ExceptionType

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def seed_dataset():
    return generate(SEED_SPEC)


@pytest.fixture(scope="module")
def full_dataset():
    return generate(FULL_SPEC)


# --------------------------------------------------------------------------- #
# The DoD
# --------------------------------------------------------------------------- #
def test_full_dataset_reaches_the_required_scale(full_dataset) -> None:
    """A.3 requires proof on 500-5,000 records."""
    assert 4_000 <= full_dataset.row_count <= 6_000, full_dataset.row_count
    assert len(full_dataset.truth) > 100


def test_all_sixteen_exception_types_are_present(full_dataset, seed_dataset) -> None:
    """Every type, in both datasets. A pipeline graded on twelve of sixteen types is
    graded on a different problem than the one we claim to solve."""
    for dataset, label in ((seed_dataset, "seed"), (full_dataset, "full")):
        missing = sorted(name for name, count in coverage(dataset).items() if count == 0)
        assert missing == [], f"{label} dataset is missing {missing}"
    assert len(list(ExceptionType)) == 16


def test_the_committed_seed_is_small_enough_to_be_instant(seed_dataset) -> None:
    assert 300 <= seed_dataset.row_count <= 700, seed_dataset.row_count


def test_the_seed_is_committed_and_matches_the_generator(seed_dataset) -> None:
    """A judge running `make demo` uses the committed files; if they drift from the
    generator, the demo and the metrics stop describing the same data."""
    manifest = json.loads((SEED_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["digest"] == seed_dataset.digest(), (
        "data/seed is stale - run `make data` and commit the result"
    )


def test_the_distribution_is_documented_in_the_manifest() -> None:
    manifest = json.loads((SEED_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["distribution"]) <= {t.value for t in ExceptionType}
    assert manifest["note"].startswith("Synthetic. No real merchant data.")


# --------------------------------------------------------------------------- #
# Label integrity - the tests that protect every metric downstream
# --------------------------------------------------------------------------- #
def test_every_truth_group_balances_to_the_paise(seed_dataset, full_dataset) -> None:
    """gross + sum(components) == net_expected, exactly, for every group.

    This is the conservation invariant (D.1.2) asserted against the *labels*. If the
    truth itself did not balance, the pipeline would be graded against arithmetic
    that is wrong, and a correct matcher would score as broken.
    """
    for dataset in (seed_dataset, full_dataset):
        unbalanced = [g.group_id for g in dataset.truth if not g.balances()]
        assert unbalanced == []


def test_the_generator_refuses_to_emit_unbalanced_labels(monkeypatch) -> None:
    """Proof the balance rule is enforced at generation time, not merely observed.

    Two guards exist: a per-group check inside `_build_settlements` that fires as
    soon as a settlement fails to tie out, and a whole-dataset sweep in `generate`
    that also covers the auxiliary groups (reserve releases, orphan credits). This
    forces the first of them, which is the one that fires earliest.
    """
    import data.gen as gen_module

    monkeypatch.setattr(gen_module.TruthGroup, "balances", lambda self: False)
    with pytest.raises(AssertionError, match="unbalanced"):
        generate(DatasetSpec(name="broken", days=3, payments_per_day=2))


def test_no_generator_hints_leak_into_the_shipped_gateway_files() -> None:
    """A real gateway response does not tell you which settlement a payment landed
    in. A matcher that read `_settles_on` would score itself on a problem nobody has.
    """
    for name in ("gateway_payments", "gateway_refunds", "gateway_settlements"):
        payload = json.loads((SEED_DIR / f"{name}.json").read_text(encoding="utf-8"))
        for item in payload["items"]:
            leaked = [key for key in item if key.startswith("_")]
            assert leaked == [], f"{name} leaks {leaked}"


def test_bank_row_ids_are_opaque() -> None:
    """`unknown-0` and `corrupt-1` as row ids would let a trivial 'matcher' read the
    prefix and score perfectly. Real statements carry opaque references."""
    import re

    rows = list(csv.DictReader((SEED_DIR / "bank_statement.csv").open(encoding="utf-8")))
    assert rows
    for row in rows:
        assert re.fullmatch(r"TR[0-9A-F]{10}", row["row_id"]), row["row_id"]
    for word in ("unknown", "corrupt", "orphan", "setl_", "rsv", "dup"):
        assert not any(word in row["row_id"].lower() for row in rows)


def test_every_truth_reference_resolves_to_a_real_row() -> None:
    """Dangling labels are worse than missing ones: they silently deflate recall."""
    bank_ids = {
        row["row_id"]
        for row in csv.DictReader((SEED_DIR / "bank_statement.csv").open(encoding="utf-8"))
    }
    ledger_ids = {
        row["invoice_no"]
        for row in csv.DictReader((SEED_DIR / "ledger_invoices.csv").open(encoding="utf-8"))
    }
    payment_ids = {
        item["id"]
        for item in json.loads((SEED_DIR / "gateway_payments.json").read_text(encoding="utf-8"))["items"]
    }
    truth = json.loads((SEED_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    for group in truth["groups"]:
        assert set(group["bank_ids"]) <= bank_ids, group["group_id"]
        assert set(group["ledger_ids"]) <= ledger_ids, group["group_id"]
        assert set(group["gateway_ids"]) <= payment_ids, group["group_id"]


def test_ground_truth_is_never_imported_by_the_matching_code() -> None:
    """Structural guarantee: recon/ must not be able to read the answers."""
    offenders = []
    for package in ("recon", "ingest", "forecast", "qa"):
        for path in sorted((ROOT / package).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "ground_truth" in text or "TruthGroup" in text:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == [], f"these modules can see the labels: {offenders}"


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_generation_is_byte_deterministic() -> None:
    assert generate(SEED_SPEC).digest() == generate(SEED_SPEC).digest()


def test_a_different_seed_gives_different_data() -> None:
    other = DatasetSpec(name="seed", days=21, payments_per_day=14, seed=99)
    assert generate(other).digest() != generate(SEED_SPEC).digest()


def test_written_files_are_byte_identical_across_runs(tmp_path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    generate(SEED_SPEC).write(first)
    generate(SEED_SPEC).write(second)
    for path in sorted(first.iterdir()):
        assert path.read_bytes() == (second / path.name).read_bytes(), path.name


# --------------------------------------------------------------------------- #
# The economics the pipeline has to recover
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method", sorted(ZERO_MDR_METHODS, key=lambda m: m.value))
def test_upi_and_rupay_debit_carry_no_mdr(method: PaymentMethod) -> None:
    """Zero-MDR methods are regulated in India. A fee model with one blended rate
    cannot reconcile an Indian merchant's books, so the generator must not use one."""
    assert MDR_BPS[method] == 0
    components = fee_components(Money.from_rupees("10000"), method)
    assert components[ExceptionType.FEE_MDR.value] == 0
    assert components[ExceptionType.GST_ON_FEE.value] == 0
    # TDS under 194-O still applies: it is on the sale, not on the fee.
    assert components[ExceptionType.TDS_194O.value] < 0


def test_gst_is_charged_on_the_fee_not_on_the_sale() -> None:
    """The classic and expensive mistake. 18% of the MDR, never 18% of the gross."""
    gross = Money.from_rupees("100000")
    components = fee_components(gross, PaymentMethod.CARD_CREDIT)
    mdr = -components[ExceptionType.FEE_MDR.value]
    gst = -components[ExceptionType.GST_ON_FEE.value]
    assert mdr == Rate.from_bps(200).of(gross).paise
    assert gst == Rate.from_percent(18).of(Money(mdr)).paise
    assert gst != Rate.from_percent(18).of(gross).paise


def test_tds_is_a_tenth_of_a_percent_of_gross() -> None:
    gross = Money.from_rupees("100000")
    components = fee_components(gross, PaymentMethod.UPI)
    assert -components[ExceptionType.TDS_194O.value] == Money.from_rupees("100").paise


def test_settlements_are_netted_many_to_one(seed_dataset) -> None:
    """The shape that makes this a real problem: a day's captures arrive as ONE
    credit, not one line per sale. Without this the matcher would only ever need
    1:1 matching and the subset-sum solver would be decoration."""
    multi = [g for g in seed_dataset.truth if len(g.gateway_ids) > 1]
    assert multi, "no many-to-one settlement groups were generated"
    biggest = max(len(g.gateway_ids) for g in multi)
    assert biggest >= 5, f"largest settlement group is only {biggest} payments"
    assert any(len(g.bank_ids) == 1 and len(g.gateway_ids) > 3 for g in multi)


def test_split_settlements_produce_more_than_one_bank_credit(seed_dataset) -> None:
    split = [g for g in seed_dataset.truth if ExceptionType.SPLIT_SETTLEMENT.value in g.anomalies]
    assert split
    assert all(len(g.bank_ids) > 1 for g in split)


def test_missing_in_bank_groups_have_no_bank_row(seed_dataset) -> None:
    missing = [g for g in seed_dataset.truth if ExceptionType.MISSING_IN_BANK.value in g.anomalies]
    assert missing
    assert all(g.bank_ids == () for g in missing)


def test_contradictory_anomalies_are_never_combined(full_dataset) -> None:
    """A settlement cannot be both short and over, nor both missing and split."""
    for group in full_dataset.truth:
        anomalies = set(group.anomalies)
        assert not {ExceptionType.SHORT_PAY.value, ExceptionType.OVER_PAY.value} <= anomalies
        assert not {
            ExceptionType.MISSING_IN_BANK.value,
            ExceptionType.SPLIT_SETTLEMENT.value,
        } <= anomalies


def test_timing_anomalies_move_the_credit_to_a_business_day(seed_dataset) -> None:
    from core.clock import INDIAN_BANK_CALENDAR

    timed = [g for g in seed_dataset.truth if ExceptionType.TIMING.value in g.anomalies]
    assert timed
    for group in timed:
        assert INDIAN_BANK_CALENDAR.is_business_day(group.settlement_date)


# --------------------------------------------------------------------------- #
# Realism of the source formats
# --------------------------------------------------------------------------- #
def test_the_three_sources_ship_in_three_different_formats() -> None:
    """Collapsing them into one shape would hide the actual ingest work."""
    assert (SEED_DIR / "gateway_payments.json").exists()
    assert (SEED_DIR / "bank_statement.csv").exists()
    assert (SEED_DIR / "ledger_invoices.csv").exists()

    payload = json.loads((SEED_DIR / "gateway_payments.json").read_text(encoding="utf-8"))
    assert payload["entity"] == "collection" and "items" in payload
    item = payload["items"][0]
    assert isinstance(item["amount"], int), "Razorpay amounts are integer paise"
    assert item["id"].startswith("pay_")
    assert isinstance(item["created_at"], int), "created_at is epoch seconds"


def test_bank_amounts_use_indian_digit_grouping() -> None:
    rows = list(csv.DictReader((SEED_DIR / "bank_statement.csv").open(encoding="utf-8")))
    grouped = [r["credit"] for r in rows if "," in r["credit"]]
    assert grouped, "no lakh-grouped amounts in the statement"
    assert any(len(part) == 2 for value in grouped for part in value.split(",")[:-1] if part), (
        "Indian grouping puts pairs before the final triple (12,40,000)"
    )


def test_the_same_entity_is_spelled_differently_across_sources(seed_dataset) -> None:
    """Stage 0's normaliser exists because of exactly this."""
    ledger_names = {row["counterparty"] for row in seed_dataset.ledger_rows}
    suffixes = {"Pvt Ltd", "Private Limited", "PVT. LTD.", "LLP", "& Co", "AND CO"}
    seen = {s for s in suffixes if any(name.endswith(s) for name in ledger_names)}
    assert len(seen) >= 3, f"only {seen} suffix variants generated"


def test_corrupt_rows_are_present_and_survive_as_rows() -> None:
    """They must be shippable to a human as evidence, not silently dropped."""
    rows = list(csv.DictReader((SEED_DIR / "bank_statement.csv").open(encoding="utf-8")))
    bad_amount = [r for r in rows if r["credit"] in {"", "1,2,3.4.5", "NULL", "N/A"}]
    bad_date = [r for r in rows if r["value_date"] in {"not-a-date", "00/00/0000", "31-02-2026"}]
    assert bad_amount, "no unparseable-amount rows in the statement"
    assert bad_date, "no unparseable-date rows in the statement"
    assert all("##TRUNCATED" in r["narration"] for r in bad_amount + bad_date)


def test_narrations_look_like_real_bank_text() -> None:
    rows = list(csv.DictReader((SEED_DIR / "bank_statement.csv").open(encoding="utf-8")))
    narrations = [r["narration"] for r in rows]
    assert any(n.startswith("NEFT-") for n in narrations)
    assert any(n.startswith("UPI/CR/") for n in narrations)
    assert any("RAZORPAY SOFTWARE PVT LTD" in n for n in narrations)
