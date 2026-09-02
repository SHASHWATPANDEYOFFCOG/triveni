"""Stage 0: canonicalise references, counterparty names and free-text narrations.

This module is where the AI-judgment rule earns its keep. The bank's narration column
is genuinely messy natural language, which is the one thing an LLM is actually good
at - but most of it is not messy at all. `NEFT-HDFC940235305794-RAZORPAY SOFTWARE PVT
LTD-SETTLEMENT 000489` is a *format*, and a format is a regex, not a prompt.

So the design is: **deterministic extraction first, and the LLM only on the residue.**
:func:`parse_narration` returns a result that knows whether it is complete. If the
regexes recovered every field, the row never reaches a model, and the pipeline records
that it did not. The resulting `llm_call_rate` metric is a first-class number in the
eval table, because "we called the LLM on 6% of rows" is a claim about engineering
judgment that can be checked rather than asserted.

The name normaliser is deliberately India-aware. `Pillai Hardware P Ltd`,
`PILLAI HARDWARE PRIVATE LIMITED` and `Pillai Hardware Pvt. Ltd` are one legal entity
spelled three ways by three systems, and no amount of edit distance fixes that as
reliably as knowing what `P Ltd` means.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

# --------------------------------------------------------------------------- #
# Counterparty names
# --------------------------------------------------------------------------- #
#: Legal-form suffixes, longest first so `PRIVATE LIMITED` is stripped before
#: `LIMITED` turns it into a stray `PRIVATE`.
_LEGAL_SUFFIXES: Final[tuple[str, ...]] = (
    "PRIVATE LIMITED",
    "PVT LTD",
    "PVT LIMITED",
    "P LTD",
    "PUBLIC LIMITED",
    "LIMITED",
    "LTD",
    "LLP",
    "OPC",
    "AND SONS",
    "AND CO",
    "AND COMPANY",
    "COMPANY",
    "CO",
    "SONS",
    "INC",
    "CORPORATION",
    "CORP",
)

#: Honorifics and address-forms that carry no identity.
_HONORIFICS: Final[frozenset[str]] = frozenset(
    {"MR", "MRS", "MS", "SHRI", "SHREE", "SMT", "SRI", "DR", "PROF", "M/S", "MS.", "MESSRS"}
)

_PUNCT = re.compile(r"[^\w\s&]")
_SPACES = re.compile(r"\s+")


def normalize_counterparty(name: str) -> str:
    """Collapse the spellings of one legal entity to a single key.

    Deliberately deterministic and explainable - a matcher that cannot say *why* two
    names are the same is a matcher a finance team will not trust. The steps are:
    strip accents, uppercase, expand ``&`` to ``AND``, drop punctuation and
    honorifics, then remove legal-form suffixes repeatedly (``Foo Pvt Ltd Co`` has
    two).
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper().replace("&", " AND ")

    # Punctuated honorifics have to go before punctuation is stripped: "M/S" becomes
    # the two tokens "M" and "S" the moment the slash is removed, and neither is in
    # the honorific set, so "M/S Pillai Hardware" would normalise to "M S PILLAI
    # HARDWARE" and never match "PILLAI HARDWARE".
    text = re.sub(r"^\s*(M/S|M/S\.|MESSRS\.?)\s+", " ", text)

    text = _PUNCT.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()

    tokens = [t for t in text.split() if t not in _HONORIFICS]
    text = " ".join(tokens)

    # Repeatedly strip trailing legal forms; one pass is not enough for "PVT LTD CO".
    changed = True
    while changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if text.endswith(" " + suffix) or text == suffix:
                text = text[: -len(suffix)].strip()
                changed = True
                break
    return _SPACES.sub(" ", text).strip()


def counterparty_tokens(name: str) -> frozenset[str]:
    """Token set of a normalised name, for blocking and for Jaccard overlap."""
    return frozenset(t for t in normalize_counterparty(name).split() if len(t) > 1)


def name_key(name: str) -> str:
    """A conservative phonetic key for blocking Indian names.

    Not a matching decision - a *candidate generation* device. Indian surnames are
    transliterated inconsistently (Chatterjee/Chatterji, Mukherjee/Mukherji,
    Krishnan/Krishnaan), and this collapses the vowel and doubled-consonant variation
    that causes most of it without the false merges a full Soundex produces on short
    tokens. Two names sharing a key are worth *comparing*; nothing more.
    """
    text = normalize_counterparty(name)
    if not text:
        return ""
    keys: list[str] = []
    for token in text.split():
        head = token[0]
        body = re.sub(r"[AEIOU]+", "", token[1:])
        body = re.sub(r"(.)\1+", r"\1", body)
        body = body.replace("PH", "F").replace("KH", "K").replace("GH", "G")
        body = body.replace("TH", "T").replace("DH", "D").replace("BH", "B")
        body = body.replace("Z", "S").replace("V", "W").replace("Q", "K")
        keys.append((head + body)[:6])
    return " ".join(keys)


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #
# No `\b` around the digits, deliberately. Banks glue their own code straight onto
# the reference - `NEFT-ICIC232424838548` - and a word boundary never matches between
# `C` and `2` because both are word characters, so the anchored version silently
# extracted nothing from the single most common real-world shape.
_UTR_RE = re.compile(r"\d{9,22}")
_RRN_RE = re.compile(r"\d{12}")
_ORDER_RE = re.compile(r"\b(order_[A-Za-z0-9]+)\b")
_PAYMENT_RE = re.compile(r"\b(pay_[A-Za-z0-9]+)\b")
_INVOICE_RE = re.compile(r"\b(INV[-/ ]?\d{4}[-/ ]?\d+)\b", re.IGNORECASE)

#: Rail prefixes banks bolt onto a reference. Stripping them is what makes the same
#: UTR from two sources compare equal.
_RAIL_PREFIXES: Final[tuple[str, ...]] = (
    "NEFT", "RTGS", "IMPS", "UPI", "ACH", "ECS", "NACH", "CHQ", "CMS", "MMT",
    "INF", "TRF", "BIL", "POS", "ATM", "CR", "DR", "REF", "TXN",
)


def normalize_reference(reference: str) -> str:
    """Reduce a reference to comparable form.

    Uppercase, drop separators, then strip leading rail markers and any bank code
    glued to the front - `NEFT-HDFC940235305794` and `940235305794` are the same
    payment and must compare equal, or Stage 1 finds nothing.
    """
    if not reference:
        return ""
    text = re.sub(r"[^\w]", "", reference.upper())
    changed = True
    while changed:
        changed = False
        for prefix in _RAIL_PREFIXES:
            if text.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix) :]
                changed = True
                break
    # A bank code sits between the rail marker and the numeric UTR: HDFC940235...
    match = re.match(r"^[A-Z]{2,6}(\d{9,})$", text)
    if match:
        text = match.group(1)
    return text


def extract_utr(text: str) -> str:
    """The longest digit run that looks like a UTR/RRN. Empty if there is none."""
    matches = _UTR_RE.findall(text or "")
    return max(matches, key=len) if matches else ""


# --------------------------------------------------------------------------- #
# Narrations
# --------------------------------------------------------------------------- #
class Rail(StrEnum):
    NEFT = "neft"
    RTGS = "rtgs"
    IMPS = "imps"
    UPI = "upi"
    CARD = "card"
    CASH = "cash"
    UNKNOWN = "unknown"


class ParseSource(StrEnum):
    """Who produced this parse. Measured, and reported in the eval table."""

    REGEX = "regex"
    """Deterministic extraction. Free, exact, auditable."""

    LLM = "llm"
    """A model was called because the regexes came up short."""

    NONE = "none"
    """Nothing could be extracted, by either route."""


@dataclass(frozen=True, slots=True)
class NarrationParse:
    """What we managed to pull out of a free-text narration, and how."""

    raw: str
    rail: Rail = Rail.UNKNOWN
    utr: str = ""
    counterparty: str = ""
    reference: str = ""
    is_settlement: bool = False
    source: ParseSource = ParseSource.REGEX
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    """Character offsets into ``raw`` for each extracted field, so the UI can
    highlight exactly where a value came from and a human can check it in one look."""

    @property
    def complete(self) -> bool:
        """True when deterministic extraction found everything that matters.

        This is the gate that decides whether a row costs an LLM call. It is
        deliberately strict about the UTR - the one field that makes a deterministic
        match possible - and lenient about the counterparty, which the name
        normaliser can often recover on its own.
        """
        return bool(self.utr) and self.rail is not Rail.UNKNOWN

    def needs_llm(self) -> bool:
        return not self.complete


_SETTLEMENT_MARKERS = ("SETTLEMENT", "PAYOUT", "RESERVE RELEASE", "RAZORPAY")

_NARRATION_PATTERNS: tuple[tuple[Rail, re.Pattern[str]], ...] = (
    (
        Rail.UPI,
        re.compile(
            r"^UPI[/-](?:CR|DR)?[/-]?(?P<utr>\d{9,22})[/-](?P<name>[^/]+)(?:[/-](?P<bank>[A-Z]{4}))?",
            re.IGNORECASE,
        ),
    ),
    (
        Rail.NEFT,
        re.compile(
            r"^NEFT[-/](?P<bankcode>[A-Z]{2,6})?(?P<utr>\d{9,22})[-/](?P<name>[^-/]+)", re.IGNORECASE
        ),
    ),
    (
        Rail.RTGS,
        re.compile(
            r"^RTGS[-/](?P<bankcode>[A-Z]{2,6})?R?(?P<utr>\d{9,22})[-/](?P<name>[^-/]+)", re.IGNORECASE
        ),
    ),
    (
        Rail.IMPS,
        re.compile(
            r"^IMPS[/-](?:P2A|P2P)?[/-]?(?P<utr>\d{9,22})[/-](?P<name>[^/]+)", re.IGNORECASE
        ),
    ),
)


def parse_narration(narration: str) -> NarrationParse:
    """Deterministically extract what a bank narration is telling us.

    Returns a parse whose ``complete`` flag says whether an LLM is needed. Most rows
    are a known format and never reach a model; the ones that do are genuinely
    ambiguous free text, which is exactly the division of labour the constitution
    asks for.
    """
    text = (narration or "").strip()
    if not text:
        return NarrationParse(raw=narration or "", source=ParseSource.NONE)

    upper = text.upper()
    is_settlement = any(marker in upper for marker in _SETTLEMENT_MARKERS)

    for rail, pattern in _NARRATION_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groupdict()
        utr = groups.get("utr") or ""
        name = (groups.get("name") or "").strip()
        spans: dict[str, tuple[int, int]] = {}
        if utr:
            spans["utr"] = match.span("utr")
        if name:
            spans["counterparty"] = match.span("name")
        return NarrationParse(
            raw=text,
            rail=rail,
            utr=utr,
            counterparty=name,
            reference=normalize_reference(utr),
            is_settlement=is_settlement,
            source=ParseSource.REGEX,
            spans=spans,
        )

    # No known format matched. A bare UTR is still worth having - it is the strongest
    # deterministic key there is - but without a rail we do not claim a complete parse.
    utr = extract_utr(text)
    if utr:
        index = text.find(utr)
        return NarrationParse(
            raw=text,
            rail=Rail.UNKNOWN,
            utr=utr,
            reference=normalize_reference(utr),
            is_settlement=is_settlement,
            source=ParseSource.REGEX,
            spans={"utr": (index, index + len(utr))},
        )

    return NarrationParse(
        raw=text, is_settlement=is_settlement, source=ParseSource.NONE
    )


@dataclass(frozen=True, slots=True)
class NarrationStats:
    """How much of the batch needed a model. Reported in the eval table."""

    total: int = 0
    regex_complete: int = 0
    needs_llm: int = 0
    empty: int = 0

    @property
    def llm_call_rate(self) -> float:
        return self.needs_llm / self.total if self.total else 0.0

    @property
    def deterministic_rate(self) -> float:
        return self.regex_complete / self.total if self.total else 0.0

    def render(self) -> str:
        return (
            f"{self.total} narrations: {self.regex_complete} parsed deterministically "
            f"({self.deterministic_rate:.1%}), {self.needs_llm} would need an LLM "
            f"({self.llm_call_rate:.1%}), {self.empty} empty"
        )


def summarise(narrations: list[str]) -> NarrationStats:
    """Measure the LLM call rate before any model is wired up.

    Knowing this number early is what lets the LLM gateway at M11 be a small, bounded
    component rather than the centre of the system.
    """
    total = complete = needs = empty = 0
    for narration in narrations:
        total += 1
        parse = parse_narration(narration)
        if parse.source is ParseSource.NONE and not narration.strip():
            empty += 1
        if parse.complete:
            complete += 1
        else:
            needs += 1
    return NarrationStats(total=total, regex_complete=complete, needs_llm=needs, empty=empty)
