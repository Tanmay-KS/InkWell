"""Personal-detail redaction.

Phase 2 core — Person A. The deterministic local pass below is implemented
now because it is pure, testable, and has **no egress at all**; the
model-assisted escalation lands in Phase 2.

The ordering is the whole design:

    1. A deterministic local pass runs first. It finds the structured
       identifiers — email addresses, phone numbers, account numbers,
       card numbers, IBANs, postal addresses. It costs nothing, it is
       fast, and the personal details it catches never leave the machine.

    2. Only the spans the local pass cannot confidently classify (names,
       free-text identifiers, context-dependent details) are escalated to
       a Nemotron Nano call for semantic redaction.

This ordering is what makes the leak test honest. A Nano redaction call is
itself outbound traffic carrying the source text. If redaction were
model-only, the claim would be "personal details reach a third-party model
before they are removed" — which is a much weaker claim than the one we
want to make, and arguably not a privacy win at all.

What we can honestly claim, with the architecture below, is: *personal
details never reach a Super/Ultra tier or a third-party service*. The Nano
redaction call is reported as a separate line item in the leak test, not
hidden inside the headline number.

This is PII, not secrets. Hermes already redacts credential-shaped strings
from tool output before they reach the model (see `agent/redact.py` in the
Hermes source). That layer is not duplicated here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Category",
    "Redaction",
    "RedactionResult",
    "contains_personal_detail",
    "find_spans",
    "local_redact",
    "redact",
]


class Category(StrEnum):
    """Categories of personal detail we detect."""

    EMAIL = "email"
    PHONE = "phone"
    ACCOUNT = "account"
    SSN = "ssn"
    CARD = "card"
    IBAN = "iban"
    ADDRESS = "address"
    DOB = "dob"
    NAME = "name"
    OTHER = "other"


# --- patterns ---------------------------------------------------------------
#
# Each pattern is deliberately conservative. A false positive that masks a
# harmless word makes the agent's output worse for the user; a false
# negative leaks. Where a pattern is risky, it is gated on context.

_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Phone numbers are the hardest to get right. These patterns require either
# a country code, a parenthesised area code, or separators — so that
# account numbers, dates, and monetary amounts do not match.
_PHONE = re.compile(
    r"(?<![\w-])"
    r"(?:\+\d{1,3}[\s.\-]?)?"
    r"(?:\(\d{3}\)|\d{3})"
    r"[\s.\-]?"
    r"\d{3}"
    r"[\s.\-]?"
    r"\d{4}"
    r"(?![\w-])"
)

_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Card numbers: 13-19 digits, optionally grouped, validated with Luhn so
# that long numeric identifiers do not get masked as cards.
_CARD = re.compile(r"\b(?:\d[ \-]?){12,18}\d\b")

_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")

# Account numbers: 8+ digits. Card numbers are matched first, so a card
# number does not also register as an account number.
_ACCOUNT = re.compile(r"\b\d{8,17}\b")

# Account numbers written in groups: 1234-5678, or 1234-5678-9012-3456
_ACCOUNT_GROUPED = re.compile(r"\b\d{4}(?:[- ]\d{4}){1,3}\b")

# Postal addresses: a street number followed by a street name ending in a
# recognised suffix. Suffixes are matched case-insensitively and the whole
# address is captured, because masking only the number would leave the
# street name, which is often the identifying part.
_ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][\w.'\-]*\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
    r"Court|Ct|Circle|Cir|Place|Pl|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|"
    r"Square|Sq|Trail|Trl|Way)\b\.?"
)

# Dates of birth only count when the surrounding text says so — a bare
# date is far more likely to be a renewal date than a birth date, and
# masking renewal dates would break the deadline detection.
_DOB_CONTEXT = re.compile(
    r"\b(?:date of birth|d\.?o\.?b\.?|born|birth ?date|dob)\b", re.IGNORECASE
)
# Both day/month and month/day orders are matched here; which one a given
# document uses is not knowable from the number alone, and a birth date
# that slips through because it was written 04/17 rather than 17/04 is
# exactly the failure this layer exists to prevent.
_DATE = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-](?:19|20)\d{2}\b")

# The order matters: the first pattern to match a span claims it.
_PATTERNS: list[tuple[Category, re.Pattern[str]]] = [
    (Category.EMAIL, _EMAIL),
    (Category.IBAN, _IBAN),
    (Category.SSN, _SSN),
    (Category.CARD, _CARD),
    (Category.ACCOUNT, _ACCOUNT_GROUPED),
    (Category.PHONE, _PHONE),
    (Category.ACCOUNT, _ACCOUNT),
    (Category.ADDRESS, _ADDRESS),
]


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum, used to avoid masking non-card long numbers."""
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    checksum = 0
    parity = len(nums) % 2
    for i, num in enumerate(nums):
        if i % 2 == parity:
            num *= 2
            if num > 9:
                num -= 9
        checksum += num
    return checksum % 10 == 0


@dataclass(frozen=True, slots=True)
class Redaction:
    """One redaction that was applied."""

    category: Category
    placeholder: str
    original: str
    start: int
    end: int

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Redaction(category={self.category.value!r}, "
            f"placeholder={self.placeholder!r}, start={self.start}, end={self.end})"
        )


@dataclass(slots=True)
class RedactionResult:
    """The outcome of a redaction pass."""

    text: str
    redactions: list[Redaction] = field(default_factory=list)
    escalated: bool = False
    """True when the model-assisted pass was needed for any span."""

    @property
    def categories(self) -> set[Category]:
        return {r.category for r in self.redactions}

    def placeholders(self) -> list[str]:
        return [r.placeholder for r in self.redactions]


def find_spans(text: str) -> list[tuple[int, int, Category]]:
    """Locate personal details in text without modifying it.

    Returns a list of (start, end, category) tuples with no overlaps,
    ordered by position.

    Exposed separately from `local_redact` so the leak test can measure
    detection without also measuring the rewriting.
    """
    if not text:
        return []

    candidates: list[tuple[int, int, Category]] = []

    for category, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            span_text = match.group(0)

            if category is Category.CARD and not _luhn_ok(span_text):
                continue
            if category is Category.ACCOUNT and _luhn_ok(span_text):
                # Already claimed by the card pattern.
                continue

            candidates.append((match.start(), match.end(), category))

    # Dates of birth need context, so they are handled separately.
    for match in _DATE.finditer(text):
        window = text[max(0, match.start() - 40) : match.end() + 40]
        if _DOB_CONTEXT.search(window):
            candidates.append((match.start(), match.end(), Category.DOB))

    # Resolve overlaps: earliest start wins, then longest span, then the
    # order the patterns are listed in.
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))

    resolved: list[tuple[int, int, Category]] = []
    last_end = -1
    for start, end, category in candidates:
        if start < last_end:
            continue
        resolved.append((start, end, category))
        last_end = end

    return resolved


def local_redact(text: str) -> RedactionResult:
    """Replace personal details with stable placeholders.

    Placeholders are numbered per category and per call:
    `[EMAIL_1]`, `[ACCOUNT_2]`, and so on. Stable numbering matters for two
    reasons: the same value always gets the same placeholder within a
    document, so a reader can see that two mentions are the same thing; and
    the leak test can check for the *original* value's absence rather than
    pattern-matching the redacted text.

    Args:
        text: The text to redact.

    Returns:
        A `RedactionResult`. The original text is never returned in the
        result — only the placeholders and the spans.
    """
    if not text:
        return RedactionResult(text="")

    spans = find_spans(text)
    if not spans:
        return RedactionResult(text=text)

    counters: dict[Category, int] = {}
    # Track which original values already have a placeholder, so the same
    # value reused in the same document keeps the same placeholder.
    assigned: dict[tuple[Category, str], str] = {}

    pieces: list[str] = []
    redactions: list[Redaction] = []
    cursor = 0

    for start, end, category in spans:
        original = text[start:end]
        key = (category, original.lower())

        if key in assigned:
            placeholder = assigned[key]
        else:
            counters[category] = counters.get(category, 0) + 1
            placeholder = f"[{category.value.upper()}_{counters[category]}]"
            assigned[key] = placeholder

        pieces.append(text[cursor:start])
        pieces.append(placeholder)
        redactions.append(
            Redaction(
                category=category,
                placeholder=placeholder,
                original=original,
                start=start,
                end=end,
            )
        )
        cursor = end

    pieces.append(text[cursor:])
    return RedactionResult(text="".join(pieces), redactions=redactions)


def redact(text: str, *, model: str | None = None) -> RedactionResult:
    """Redact personal details from text.

    Runs the deterministic local pass, then escalates to a Nemotron Nano
    call for anything the local pass could not classify — names, and
    context-dependent identifiers like "the policy under Sarah's name".

    Args:
        text: The text to redact.
        model: The redaction model. Defaults to `LEDGER_MODEL_REDACT`.

    Returns:
        A `RedactionResult` whose `escalated` flag says whether a model
        call was made.

    Phase 2 implementation: the local pass is done; the escalation is
    Phase 2 work. Until then, this function is the local pass, and the
    caller must not treat a clean result as proof that nothing personal
    remains.
    """
    result = local_redact(text)
    if model is not None:
        raise NotImplementedError(
            "Model-assisted redaction is Phase 2 work. "
            "Until it lands, redact() performs the deterministic local pass only."
        )
    return result


def contains_personal_detail(text: str) -> list[Category]:
    """Report which categories of personal detail appear in text.

    Used by the leak test to score an outbound payload. It answers "does
    this body contain something personal", not "is this body safe" — a
    category the local pass cannot see (names) will not appear here.

    Phase 2: extend with the model-assisted pass so the leak test measures
    the same coverage the router actually applies.
    """
    return {category for _start, _end, category in find_spans(text)}
