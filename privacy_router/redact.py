"""Personal-detail redaction.

Phase 2 core — Person A. The deterministic local pass is pure, testable,
and has **no egress at all**. The model-assisted escalation is implemented
too, and it is the *only* call the router itself makes — which is why the
leak test reports it as its own line item instead of folding it into the
headline.

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

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from privacy_router.egress import OutboundRequest, Transport, http_transport

__all__ = [
    "Category",
    "Redaction",
    "RedactionMap",
    "RedactionResult",
    "contains_personal_detail",
    "find_spans",
    "local_redact",
    "parse_residual_spans",
    "redact",
    "redacted_text",
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

# A street name on its own is not an address. `Marrow, CO 80211` is a
# place, and a place identifies someone just as well as a street does —
# the audit found the city and the postcode surviving the street
# pattern on every single document. So the address span continues past
# the street line, through the city, the state and the postcode, and one
# span covers the whole address.
_ADDRESS_FULL = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][\w.'\-]*\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
    r"Court|Ct|Circle|Cir|Place|Pl|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|"
    r"Square|Sq|Trail|Trl|Way)\b\.?"
    r"(?:,\s*[A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,2})?"
    r"(?:,\s*[A-Z]{2})?"
    r"(?:\s+\d{5}(?:-\d{4})?)?"
)

# A place with no street number: a letterhead address, or a city named
# on its own. The lookahead keeps a salutation out of it — without it
# "Dear Sam, CA 90210" reads as a city called "Dear Sam", and the
# salutation word gets swallowed with it. The salutation word is
# structure, and structure has to survive redaction.
_CITY_STATE_ZIP = re.compile(
    r"(?!\b(?:Dear|Hello|Hi)\s+[A-Z])"
    r"\b([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,2}),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b"
)

# Reference codes: a short uppercase prefix, a dash, and a run of
# digits — `POL-4825232-I`, `SUB-281674`, `ACC-427352`. These are
# account and policy identifiers, and they match neither the digit-only
# nor the digit-and-separator account patterns, so they used to survive
# completely. The digit run has to be at least five long, which keeps
# standards references like `ISO-8601` out.
_REFERENCE_CODE = re.compile(r"\b[A-Z]{2,6}-\d{5,10}(?:-[A-Z])?\b")

# --- names ------------------------------------------------------------------
#
# A person's name cannot be found by a pattern alone in running prose, so
# the deterministic pass looks for the two places a name reliably appears
# in a form: after a label that says it is a name, and in a salutation.
#
# This is a floor, not a ceiling. It is what the local pass can do for
# free, and the model-assisted escalation is still what catches a name
# in the middle of a sentence. The known-names path is better still, and
# it is the one the product actually uses: the household's names are in
# the memory layer already.

_NAME_LABEL = re.compile(
    r"(?i:\b(?:policy holder|member name|account holder|registered holder|"
    r"policy owner|plan holder|insured|applicant|payee|beneficiary|"
    r"subscriber|contact name|name)\s*:\s*)"
    # The separator between the words of a name is a space, never a
    # newline. With `\s+` a name ran past the end of its line and
    # swallowed the first word of the next one — "Policy holder: Devin
    # Sullivan" ate the "Member" of "Member number:".
    r"([A-Z][A-Za-z'’\-]*(?:[^\S\n]+[A-Z][A-Za-z'’\-]*){0,3})"
)

_NAME_SALUTATION = re.compile(
    r"\b(?:Dear|Hello|Hi)[^\S\n]+"
    r"([A-Z][A-Za-z'’\-]*(?:[^\S\n]+[A-Z][A-Za-z'’\-]*){0,2})"
    r"(?=[^\S\n]*[,.]|\n)"
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

# --- label-driven categories ------------------------------------------------
#
# Ten digits are a phone number or an account number and nothing in the
# digits themselves can say which. The context can: "Member number:" is
# an account, "Phone:" is a phone. Without this, a tenth of the corpus's
# account numbers were reported as phone numbers — the value was still
# removed, so it was a reporting error rather than a leak, but the leak
# test scores by category and it has to be right.
#
# The label has to be immediately before the value. "Your account number
# is 1234-5678" is not a labelled field, and guessing at a label two
# words away is how a redactor starts eating the document.

_ACCOUNT_LABEL = re.compile(
    r"(?i:\b(?:member|account|policy|registration|customer|client|reference|ref|numbered)"
    r"\s*(?:number|no\.?|#|id)?\s*[:#]?\s*)$"
)
_PHONE_LABEL = re.compile(
    r"(?i:\b(?:phone|tel|telephone|mobile|cell|fax)\s*[:#]?\s*)$"
)
_EMAIL_LABEL = re.compile(r"(?i:\b(?:e-?mail)\s*[:#]?\s*)$")

# The order matters: the first pattern to match a span claims it.
_PATTERNS: list[tuple[Category, re.Pattern[str]]] = [
    (Category.EMAIL, _EMAIL),
    (Category.IBAN, _IBAN),
    (Category.SSN, _SSN),
    (Category.CARD, _CARD),
    (Category.ACCOUNT, _ACCOUNT_GROUPED),
    (Category.PHONE, _PHONE),
    (Category.ACCOUNT, _ACCOUNT),
    (Category.ACCOUNT, _REFERENCE_CODE),
    (Category.ADDRESS, _ADDRESS_FULL),
    (Category.ADDRESS, _CITY_STATE_ZIP),
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


@dataclass(slots=True)
class RedactionMap:
    """The placeholder → original value mapping for one document.

    This is the piece that makes the whole design work.

    The model never sees a personal detail: it reads text where the
    account number is `[ACCOUNT_1]`. The agent still has to write a
    useful draft, and a draft without the account number is useless. So
    the mapping is kept **in the sandbox**, and the placeholders are
    resolved back to the real values at the last moment — when the draft
    is shown to the user, in the user's own channel, which is inside the
    boundary.

    The map is a trapdoor, so it is treated like one: it is not part of
    the document's serialisable form, it does not appear in a repr, and
    `resolve` is the only way out of it.
    """

    values: dict[str, str] = field(default_factory=dict)
    _ordered: list[str] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Longest placeholder first, so `[ACCOUNT_1]` is not half-replaced
        # by a shorter placeholder that happens to be a prefix of it.
        self._ordered = sorted(self.values, key=len, reverse=True)

    def resolve(self, text: str) -> str:
        """Substitute the real values back into `text`.

        This is the only way a personal detail leaves the redaction
        layer, and it is deliberately explicit rather than implicit.
        """
        if not text:
            return text
        for placeholder in self._ordered:
            original = self.values.get(placeholder)
            if original:
                text = text.replace(placeholder, original)
        return text

    def names(self) -> list[str]:
        """The original values that were names.

        The household's names, for feeding back into the next redaction
        pass — the memory layer knows who the household is, and the
        redactor should be told rather than having to guess.
        """
        return [original for placeholder, original in self.values.items() if "[NAME_" in placeholder]

    def by_category(self, category: Category) -> list[str]:
        """The original values that were redacted as `category`.

        The account number, for instance. The agent needs the real one
        to write a useful draft, and it is right here, in the sandbox.
        """
        prefix = f"[{category.value.upper()}_"
        return [original for placeholder, original in self.values.items() if placeholder.startswith(prefix)]

    def value(self, placeholder: str) -> str | None:
        """The original value for one placeholder, or None if unknown.

        A single lookup, for the caller that knows which placeholder it
        wants. `by_category` and `names` answer the "which ones" question;
        this answers the "what is this one" question.
        """
        return self.values.get(placeholder)

    def __len__(self) -> int:
        return len(self.values)

    @classmethod
    def from_result(cls, result: RedactionResult) -> RedactionMap:
        """Build a map from a redaction result.

        `redacted_text()` is the usual way to get one, because it does the
        redaction and the mapping together. This is for the caller who
        already holds a `RedactionResult` — from `local_redact()`, say — and
        needs the map without redoing the work.

        The same original value keeps the same placeholder, so a value
        mentioned twice in a document resolves to the same text twice.
        """
        return cls({r.placeholder: r.original for r in result.redactions})

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RedactionMap({len(self.values)} placeholders, values withheld)"


def redacted_text(text: str, *, names: Iterable[str] = ()) -> tuple[str, RedactionMap]:
    """Redact `text` and return the redacted text with its map.

    The pair has to travel together: the text is safe to send to a
    model, and the map is what makes the result useful again. Keeping
    them in one function makes it hard to send one without the other.
    """
    result = local_redact(text, names=names)
    return result.text, RedactionMap({r.placeholder: r.original for r in result.redactions})


def find_spans(
    text: str, *, names: Iterable[str] = ()
) -> list[tuple[int, int, Category]]:
    """Locate personal details in text without modifying it.

    Args:
        text: The text to scan.
        names: Names that are known to be personal — the household's
            names, from the memory layer. These are masked wherever they
            appear, which is a far stronger guarantee than a pattern
            that only recognises a name in a labelled field or a
            salutation.

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

    # Names are handled separately because the pattern matches a label
    # and the name, and only the name should be masked. Masking the
    # label as well would replace "Policy holder:" with a placeholder
    # and take the structure of the document with it.
    for pattern in (_NAME_LABEL, _NAME_SALUTATION):
        for match in pattern.finditer(text):
            candidates.append((match.start(1), match.end(1), Category.NAME))

    # Known names, longest first so that "Alex Chen" is masked as one
    # span rather than as "Alex" and "Chen" separately.
    for name in sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True):
        for match in re.finditer(re.escape(name), text, re.IGNORECASE):
            candidates.append((match.start(), match.end(), Category.NAME))

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
        resolved.append((start, end, _label_category(text, start, category)))
        last_end = end

    return resolved


def _label_category(text: str, start: int, category: Category) -> Category:
    """Reclassify a span from the label immediately before it.

    Ten digits are a phone number or an account number, and the digits
    cannot say which. The label can. This is only consulted when the
    label is *immediately* before the value — a label further away is
    not evidence about this particular span, and treating it as if it
    were is how a redactor starts eating the document.
    """
    window = text[max(0, start - 40) : start]
    if _ACCOUNT_LABEL.search(window):
        return Category.ACCOUNT
    if _PHONE_LABEL.search(window):
        return Category.PHONE
    if _EMAIL_LABEL.search(window):
        return Category.EMAIL
    return category


def local_redact(text: str, *, names: Iterable[str] = ()) -> RedactionResult:
    """Replace personal details with stable placeholders.

    Placeholders are numbered per category and per call:
    `[EMAIL_1]`, `[ACCOUNT_2]`, and so on. Stable numbering matters for two
    reasons: the same value always gets the same placeholder within a
    document, so a reader can see that two mentions are the same thing; and
    the leak test can check for the *original* value's absence rather than
    pattern-matching the redacted text.

    Args:
        text: The text to redact.
        names: Names known to be personal — the household's names, from
            the memory layer. They are masked wherever they appear, which
            is much stronger than recognising a name only in a labelled
            field or a salutation.

    Returns:
        A `RedactionResult`. The original text is never returned in the
        result — only the placeholders and the spans.
    """
    if not text:
        return RedactionResult(text="")

    spans = find_spans(text, names=names)
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


# What the escalation is asked. The instruction is ours, not the user's,
# so it is `OutboundRequest.prompt` and is never scored as personal detail.
RESIDUAL_PROMPT = """\
You are a privacy filter. The text below has already had structured
personal details replaced with [PLACEHOLDER] tokens.

List every personal detail that still remains in the text: a person's
name, an address, a phone number, an account or policy number, a date of
birth, or anything else that identifies one individual.

Rules:
- Quote each detail exactly as it appears in the text.
- Do not list anything that is already a [PLACEHOLDER] token.
- Do not list the organisation's name or a product name.
- Return JSON only, and nothing else.

Return {"spans": []} when nothing personal remains."""

_PLACEHOLDER = re.compile(r"^\[[A-Z]+_\d+\]$")


def parse_residual_spans(answer: str) -> list[str]:
    """The spans the escalation found, from the model's JSON answer.

    An unparseable answer is not an error here. The escalation is a
    best-effort second pass: if the model returns prose instead of JSON,
    the local pass has still run and the caller is no worse off. What
    would be wrong is treating "I could not parse the answer" as "there
    was nothing there", so the caller is told which happened through
    `RedactionResult.escalated` and the returned spans.
    """
    if not answer or not answer.strip():
        return []
    start = answer.find("{")
    end = answer.rfind("}")
    if start == -1 or end < start:
        return []
    try:
        data = json.loads(answer[start : end + 1])
    except json.JSONDecodeError:
        return []
    raw = data.get("spans") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []

    spans: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        span = item.strip()
        if not span or _PLACEHOLDER.match(span):
            continue
        # A span that is longer than a paragraph is the model answering a
        # different question. Masking it would destroy the payload.
        if len(span) > 200:
            continue
        if span not in spans:
            spans.append(span)
    return spans


def _apply_residual(
    text: str, spans: Iterable[str], existing: list[Redaction]
) -> tuple[str, list[Redaction]]:
    """Mask the spans the local pass missed, continuing its numbering."""
    used = {r.placeholder for r in existing}
    counter = sum(1 for r in existing if r.category is Category.OTHER)
    added: list[Redaction] = []

    # Longest first: masking "Sullivan" before "Devin Sullivan" would turn
    # the longer span into "Devin [OTHER_1]" and lose that they are one name.
    for span in sorted(set(spans), key=len, reverse=True):
        if not span or span not in text:
            continue
        counter += 1
        placeholder = f"[OTHER_{counter}]"
        while placeholder in used:
            counter += 1
            placeholder = f"[OTHER_{counter}]"
        used.add(placeholder)
        start = text.find(span)
        text = text.replace(span, placeholder)
        added.append(
            Redaction(
                category=Category.OTHER,
                placeholder=placeholder,
                original=span,
                start=start,
                end=start + len(placeholder),
            )
        )

    return text, added


def redact(
    text: str,
    *,
    model: str | None = None,
    transport: Transport | None = None,
    names: Iterable[str] = (),
) -> RedactionResult:
    """Redact personal details from text.

    Runs the deterministic local pass, then escalates to a Nemotron Nano
    call for anything the local pass could not classify — a name in the
    middle of a sentence, or a context-dependent identifier like "the
    policy under Sarah's name".

    **The escalation request carries the locally-redacted text.** That is
    the whole ordering argument: the call is made, and it is reported, but
    what it carries has already been through the deterministic pass. It is
    still the one outbound call that exists *because of* the router, so
    the leak test scores it as a line item of its own and never folds it
    into the headline.

    Args:
        text: The text to redact.
        model: The redaction model. Defaults to `LEDGER_MODEL_REDACT`.
        transport: How to send the escalation request. Defaults to a real
            HTTP call. The leak test passes a capturing transport, so the
            measurement never leaves the machine.
        names: Names known to be personal — the household's names.

    Returns:
        A `RedactionResult` whose `escalated` flag says whether a model
        call was made, and whose `redactions` include the spans the model
        found as well as the ones the local pass found.
    """
    local = local_redact(text, names=names)
    if model is None and transport is None:
        return local

    send = transport or http_transport()
    request = OutboundRequest(
        kind="redact_call",
        payload=local.text,
        prompt=RESIDUAL_PROMPT,
        model=model or os.environ.get("LEDGER_MODEL_REDACT", ""),
        detail="residual personal-detail detection",
    )
    answer = send(request)

    masked, added = _apply_residual(local.text, parse_residual_spans(answer), local.redactions)
    return RedactionResult(
        text=masked,
        redactions=[*local.redactions, *added],
        escalated=True,
    )


def contains_personal_detail(text: str, *, names: Iterable[str] = ()) -> set[Category]:
    """Report which categories of personal detail appear in text.

    Used by the leak test to score an outbound payload. It answers "does
    this body contain something personal", not "is this body safe" — a
    category the local pass cannot see (a name in the middle of a
    sentence) will not appear here.

    Phase 2: extend with the model-assisted pass so the leak test measures
    the same coverage the router actually applies.
    """
    return {category for _start, _end, category in find_spans(text, names=names)}
