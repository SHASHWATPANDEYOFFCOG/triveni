"""Generate the offline cassettes `make demo` replays.

    python -m scripts.record_cassettes

**These are synthetic fixtures, not recordings of a live model, and every entry says
so.** This build has no API key, and writing invented outputs into a file labelled
"recorded" would be exactly the dishonesty rule A.6 forbids. `TRIVENI_LLM_MODE=record`
with a key writes real ones through the same gateway and the same keys.

The stand-in that produces each fixture reads **only what the model itself would see** -
the row description and the rejected candidates - and never the ground truth. That
constraint is what stops the cassettes from quietly turning the demo into a rehearsal:
a fixture derived from the labels would make the residue stage look perfect while
proving nothing. As a result the stand-in is wrong sometimes, and the pipeline abstains
on those rows, which is the behaviour worth demonstrating anyway.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from core.llm import LLMGateway, LLMMode, load_prompts

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = "seed-fixtures"


def stand_in_classification(row_text: str, candidates_text: str) -> dict[str, Any]:
    """A deterministic stand-in for the model, reading only the model's own inputs.

    Rule-based on purpose. It is not pretending to be a language model; it is
    producing a schema-valid answer of the kind a competent one would give, so the
    offline demo exercises the *plumbing* - schema validation, k-sample agreement,
    abstention, metering - without anybody having to believe a fabricated transcript.
    """
    text = row_text.lower()
    amount_match = re.search(r"amount:\s*[^0-9]*([\d,]+)", text)
    has_candidates = "no plausible candidate" not in candidates_text.lower()

    if "reserve release" in text:
        return _answer("rolling_reserve", 0.86, "the narration names a reserve release",
                       "Confirm the matching withholding on an earlier settlement.")
    if "misc credit" in text or "ref na" in text:
        return _answer("unknown", 0.30, "the narration identifies neither a rail nor a reference",
                       "Ask the bank for the originating reference.")
    if "source: bank" in text and not has_candidates:
        return _answer("missing_in_ledger", 0.74,
                       "a bank credit with no candidate on the gateway or ledger side",
                       "Check for an unrecorded sale or a non-gateway transfer.")
    if "source: gateway" in text and not has_candidates:
        return _answer("missing_in_bank", 0.71,
                       "a captured payment with no credit that could contain it",
                       "Check whether the settlement is still in transit.")
    if "kind: refund" in text:
        return _answer("refund_offset", 0.68, "the row is a refund, netted against a cycle",
                       "Confirm which settlement absorbed it.")
    if amount_match and has_candidates:
        return _answer("timing", 0.62,
                       "a candidate of a similar amount exists on a nearby date",
                       "Check the settlement calendar for a holiday or weekend shift.")
    return _answer("unknown", 0.35, "nothing in the row distinguishes a category",
                   "Open the evidence bundle and compare against the rejected candidates.")


def _answer(kind: str, confidence: float, reason: str, action: str) -> dict[str, Any]:
    return {
        "exception_type": kind,
        "confidence": confidence,
        "reason": reason,
        "suggested_action": action,
    }


def build(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="record-cassettes")
    parser.add_argument(
        "--specs",
        default="seed,full,history",
        help="comma-separated datasets to record for, in one accumulating pass",
    )
    args = parser.parse_args(argv)

    from recon.pipeline import reconcile

    bundle_path = ROOT / "prompts" / "cassettes" / f"{BUNDLE}.json"
    bundle_path.unlink(missing_ok=True)

    def responder(prompt_name: str, rendered: str) -> dict[str, Any]:
        # The stand-in sees exactly what the model would: the rendered prompt, which
        # contains the row and the candidates and nothing else. It has no access to
        # the ground truth, and that constraint is what stops these fixtures turning
        # the demo into a rehearsal.
        return stand_in_classification(rendered, rendered)

    # One gateway across every dataset, so the recordings ACCUMULATE into a single
    # bundle. Recording per-dataset and writing each time replaced the bundle, which
    # meant `make bench --full` and the forecast dataset both hit a cassette miss -
    # the offline guarantee failing loudly, correctly, on data nobody had recorded.
    gateway = LLMGateway(mode=LLMMode.RECORD, prompts=load_prompts(), stand_in=responder)

    total_residue = 0
    for spec in [item.strip() for item in args.specs.split(",") if item.strip()]:
        directory = ROOT / "data" / ("seed" if spec == "seed" else f"generated/{spec}")
        if not directory.exists():
            print(f"  skipping {spec}: {directory.relative_to(ROOT)} does not exist")
            continue
        before = len(gateway.cassette)
        result = reconcile(directory=directory, llm=gateway)
        residue = result.escalation.considered if result.escalation else 0
        total_residue += residue
        print(
            f"  {spec:<9} {len(result.rows):>5} rows · {residue:>3} residue row(s) "
            f"· +{len(gateway.cassette) - before} new fixture(s)"
        )

    print()
    print(gateway.report())
    print()
    print(f"residue rows escalated : {total_residue}")
    print(f"denied for injection   : {gateway.meter.denials} (no call made, nothing recorded)")
    print(f"fixtures written       : {len(gateway.cassette)}")
    print(f"  -> {bundle_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
