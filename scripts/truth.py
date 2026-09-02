"""Reading ground truth correctly.

Lives in `scripts/` because nothing in `recon/` may import it. The single
function here decides what counts as a true pair, which turns out to be the most
consequential definition in the whole evaluation: get it wrong and every recall
number downstream is measured against a denominator that does not exist.
"""

from __future__ import annotations


def true_pairs_from_group(group: dict) -> set[tuple[str, str]]:
    """The cross-source pairs one settlement group actually asserts.

    Three relations, and only three:

    * **payment to invoice** - explicitly 1:1, read from the group's ``links``. This
      is the correction that matters. Taking the cross product of the group's payments
      and invoices instead would claim 196 pairs for a 14-payment settlement when
      there are 14; the other 182 are rows that merely share a settlement date. That
      error inflated the recall denominator roughly fifteen-fold and made a blocker
      with perfect coverage look like it was losing two thousand pairs.
    * **payment to bank credit** - genuinely many-to-one: the credit is their net.
    * **invoice to bank credit** - likewise.

    Plus the gateway's own settlement record to the bank credit it produced, which is
    the strongest relation in the dataset because the two share a UTR.
    """
    pairs: set[tuple[str, str]] = set()

    def add(a: str, b: str) -> None:
        pairs.add((a, b) if a < b else (b, a))

    for payment_id, invoice_id in group.get("links", []):
        add(payment_id, invoice_id)
    for bank_id in group["bank_ids"]:
        for payment_id in group["gateway_ids"]:
            add(payment_id, bank_id)
        for invoice_id in group["ledger_ids"]:
            add(invoice_id, bank_id)
        for settlement_id in group.get("settlement_ids", []):
            add(settlement_id, bank_id)
    return pairs
