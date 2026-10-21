"""Deadline detection.

Phase 1 — Person B.

Turns extracted document text into dated obligations. This is where the
plan's "document in → deadline flagged" arrow actually lives.

The output is not a notification. It is a `Deadline` record: a claim that
this document contains an obligation, with the evidence for it. The
evidence matters — when the user asks "why are you telling me this?", the
agent has to point at the line in the document that said it.

**Every deadline carries the exact text it came from.** A deadline with
no evidence is a hallucination waiting to happen, and the cheapest way to
avoid hallucinating is to never produce a claim you cannot point at. So
the deterministic pass only ever reports a date it found *in the text*,
and the model pass is required to quote the span it is claiming.

The deterministic pass runs first and is the primary one. It costs
nothing, it has no egress, and it is right far more often than a model
on a document that says "your policy renews on 2027-03-07".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

MONTH_ALTERNATION = "|".join(sorted(MONTHS, key=len, reverse=True))


class DeadlineKind(StrEnum):
    """What kind of obligation this is."""

    RENEWAL = "renewal"
    EXPIRY = "expiry"
    CANCELLATION_WINDOW = "cancellation_window"
    PAYMENT_DUE = "payment_due"
    TRIAL_END = "trial_end"
    NOTICE_PERIOD = "notice_period"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Deadline:
    """A dated obligation found in a document.

    Attributes:
        kind: What sort of obligation.
        title: A short human-readable description.
        date: The date it falls on.
        urgency_days: How many days before `date` the user should be told.
            This is not always "a week" — a cancellation window is a
            deadline the day the window *opens*, not the day it closes.
        subject: The household item this belongs to, if known.
        evidence: The exact span of source text that supports the claim.
            Always required. A deadline with no evidence is a hallucination
            waiting to happen.
        confidence: 0..1.
        metadata: Amounts, provider, reference numbers.
    """

    kind: DeadlineKind
    title: str
    date: date
    urgency_days: int = 7
    subject: str | None = None
    evidence: str = ""
    confidence: float = 0.0
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def is_past(self) -> bool:
        """Whether the date has already passed.

        A document can arrive late. The obligation is still there.
        """
        return bool(self.metadata.get("is_past", False))

    def days_until(self, today: date) -> int:
        return (self.date - today).days


# --- date recognition -------------------------------------------------------

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH_YEAR = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_ALTERNATION})\.?\s*,?\s*(\d{{4}})\b",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR = re.compile(
    rf"\b({MONTH_ALTERNATION})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})\b",
    re.IGNORECASE,
)
_NUMERIC = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
_MONTH_YEAR = re.compile(rf"\b({MONTH_ALTERNATION})\.?\s+(\d{{4}})\b", re.IGNORECASE)
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_ALTERNATION})\b", re.IGNORECASE
)
_RELATIVE = re.compile(
    r"\b(?:in|within|after)\s+(\d{1,3})\s+(day|days|week|weeks|month|months|year|years)\b",
    re.IGNORECASE,
)

# The words that say what kind of obligation a date is. Checked against
# the sentence the date appears in, not the whole document.
_KIND_WORDS: list[tuple[DeadlineKind, re.Pattern[str]]] = [
    (DeadlineKind.CANCELLATION_WINDOW, re.compile(r"\bcancel|\bcancellation|\bnotify\b", re.I)),
    (DeadlineKind.TRIAL_END, re.compile(r"\btrial\b|\bfree period\b", re.I)),
    (DeadlineKind.RENEWAL, re.compile(r"\brenew|\brenewal|\brenews\b|\bauto-?renew", re.I)),
    (DeadlineKind.EXPIRY, re.compile(r"\bexpire|\bexpiry|\bexpires\b|\blapse|\blapses\b", re.I)),
    (DeadlineKind.PAYMENT_DUE, re.compile(r"\bdue\b|\bpayment\b|\bpayable\b|\binvoice\b|\bcharge", re.I)),
    (DeadlineKind.NOTICE_PERIOD, re.compile(r"\bnotice period\b|\bnotify us\b", re.I)),
]


def _sentence_around(text: str, start: int, end: int) -> str:
    """The evidence for a deadline: the line it appears on.

    A line, not a sentence. These documents are line-oriented — a
    renewal notice is a set of labelled lines, not paragraphs — and
    splitting on "." alone runs the evidence across three unrelated
    lines. That is how "Date of issue: 2026-02-26" ends up classified as
    an expiry: the evidence window reached down to "Date of expiry:" and
    borrowed its keyword.

    The line is widened to the neighbouring lines only when the line
    itself says nothing about what the date is for.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    line = text[line_start:line_end].strip()

    if line and _KIND_WORDS and any(pattern.search(line) for _kind, pattern in _KIND_WORDS):
        return line

    # Nothing in the line says what this is. Look at the lines around
    # it — the sentence may have started above.
    context_start = text.rfind(".", 0, start)
    context_start = 0 if context_start == -1 else context_start + 1
    context_end = text.find(".", end)
    context_end = len(text) if context_end == -1 else context_end + 1
    context = text[context_start:context_end].strip()

    return context if len(context) <= len(line) + 200 else line


# A date that is not an obligation. "Date of issue" is when the document
# was written, not a deadline.
_NOT_A_DEADLINE = re.compile(
    r"\bdate of issue\b|\bissued on\b|\bdate of birth\b|\bd\.?o\.?b\.?\b|\binvoice date\b",
    re.IGNORECASE,
)


def _classify(evidence: str) -> DeadlineKind:
    """What kind of obligation a date is, from the words around it."""
    for kind, pattern in _KIND_WORDS:
        if pattern.search(evidence):
            return kind
    return DeadlineKind.UNKNOWN


def normalize_date(raw: str, *, today: date | None = None) -> date | None:
    """Parse a date out of a span of text.

    Handles the formats that actually appear in these documents: ISO
    dates, "15 January 2027", "01/15/2027", "Jan 15, 2027", "15 Jan", and
    relative language like "30 days from now" or "in 3 months".

    Returns None when the text is not a date. Ambiguous numeric formats
    (01/02/2027) are resolved using `today` to pick the interpretation
    that is not in the past, and the resolution is recorded so the
    confidence score can reflect the guess.
    """
    today = today or date.today()
    text = raw.strip()

    match = _ISO.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    match = _DAY_MONTH_YEAR.search(text)
    if match:
        month = MONTHS[match.group(2).lower()]
        try:
            return date(int(match.group(3)), month, int(match.group(1)))
        except ValueError:
            return None

    match = _MONTH_DAY_YEAR.search(text)
    if match:
        month = MONTHS[match.group(1).lower()]
        try:
            return date(int(match.group(3)), month, int(match.group(2)))
        except ValueError:
            return None

    match = _NUMERIC.search(text)
    if match:
        first, second, year = (int(g) for g in match.groups())
        return _resolve_numeric(first, second, year, today)

    match = _MONTH_YEAR.search(text)
    if match:
        month = MONTHS[match.group(1).lower()]
        return _resolve_month_year(month, int(match.group(2)), today)

    match = _DAY_MONTH.search(text)
    if match:
        month = MONTHS[match.group(2).lower()]
        return _resolve_day_month(int(match.group(1)), month, today)

    match = _RELATIVE.search(text)
    if match:
        return _resolve_relative(int(match.group(1)), match.group(2).lower(), today)

    return None


def _resolve_numeric(first: int, second: int, year: int, today: date) -> date | None:
    """Resolve `01/02/2027`, which could be either order.

    If one of the two is greater than 12 the order is not ambiguous. If
    both could be, the interpretation that is not in the past wins — a
    notice saying "01/02/2027" on 20 January 2027 means 1 February, not
    2 January.
    """
    try:
        if first > 12:
            return date(year, second, first)
        if second > 12:
            return date(year, first, second)
    except ValueError:
        return None

    candidates = []
    for month, day in ((first, second), (second, first)):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None

    future = [c for c in candidates if c >= today]
    if future:
        return min(future)
    return max(candidates)


def _resolve_month_year(month: int, year: int, today: date) -> date | None:
    """`January 2027` — the first day of the month, or the last day?"""
    try:
        first = date(year, month, 1)
    except ValueError:
        return None
    if first >= today:
        return first
    # The month has already started, so the obligation is at the end of it.
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _resolve_day_month(day: int, month: int, today: date) -> date | None:
    """`15 Jan` with no year — the next one that has not passed."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _resolve_relative(count: int, unit: str, today: date) -> date | None:
    """`in 30 days`, `within 2 weeks`, `after 3 months`."""
    if unit.startswith("day"):
        return today + timedelta(days=count)
    if unit.startswith("week"):
        return today + timedelta(weeks=count)
    if unit.startswith("month"):
        return today + timedelta(days=30 * count)
    if unit.startswith("year"):
        return today + timedelta(days=365 * count)
    return None


# --- finding deadlines ------------------------------------------------------


def _candidate_spans(text: str) -> list[tuple[int, int]]:
    """Every span of text that looks like a date, without duplicates.

    Longer, more specific patterns are tried first and a span is only
    taken once, so `15 January 2027` is not also read as `15 January`.
    """
    spans: list[tuple[int, int]] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < c_end and c_start < end for c_start, c_end in claimed)

    for pattern in (_ISO, _DAY_MONTH_YEAR, _MONTH_DAY_YEAR, _NUMERIC, _MONTH_YEAR, _DAY_MONTH):
        for match in pattern.finditer(text):
            if not overlaps(match.start(), match.end()):
                spans.append((match.start(), match.end()))
                claimed.append((match.start(), match.end()))

    for match in _RELATIVE.finditer(text):
        if not overlaps(match.start(), match.end()):
            spans.append((match.start(), match.end()))

    return sorted(spans)


def _confidence(raw: str, kind: DeadlineKind) -> float:
    """How much to trust this deadline."""
    if _ISO.search(raw):
        score = 1.0
    elif _DAY_MONTH_YEAR.search(raw) or _MONTH_DAY_YEAR.search(raw):
        score = 0.95
    elif _NUMERIC.search(raw):
        score = 0.7  # the order was a guess
    elif _RELATIVE.search(raw):
        score = 0.6  # relative to the reference date
    elif _MONTH_YEAR.search(raw):
        score = 0.5
    else:
        score = 0.6

    if kind is DeadlineKind.UNKNOWN:
        score -= 0.2
    return max(0.0, min(1.0, score))


def flag_deadlines(
    text: str,
    *,
    today: date | None = None,
    model: str | None = None,
) -> list[Deadline]:
    """Find the dated obligations in a document.

    Args:
        text: The extracted document text. Redacted — the personal
            details are already placeholders, which does not affect date
            detection.
        today: The reference date, for "in 30 days" style language and for
            deciding which deadlines are already past.
        model: The model to use. Defaults to `LEDGER_MODEL_SUPER` — this
            stage needs real reasoning, not the cheap tier. Unused on the
            deterministic path, which is the primary one.

    Returns:
        Deadlines sorted by date, soonest first. Past deadlines are
        included but flagged, because a document that arrived late may
        still contain an obligation the user has to act on.
    """
    if not text or not text.strip():
        return []

    today = today or date.today()
    deadlines: list[Deadline] = []

    for start, end in _candidate_spans(text):
        raw = text[start:end]
        parsed = normalize_date(raw, today=today)
        if parsed is None:
            continue

        evidence = _sentence_around(text, start, end)
        if _NOT_A_DEADLINE.search(evidence):
            # "Date of issue" is when the document was written.
            continue

        kind = _classify(evidence)
        confidence = _confidence(raw, kind)

        deadlines.append(
            Deadline(
                kind=kind,
                title=_title_for(kind, evidence),
                date=parsed,
                urgency_days=_urgency_for(kind),
                evidence=evidence or raw,
                confidence=confidence,
                metadata={
                    "matched_text": raw,
                    "is_past": parsed < today,
                    "days_until": (parsed - today).days,
                },
            )
        )

    deadlines = _deduplicate(deadlines)
    deadlines.sort(key=lambda d: (d.date, d.kind.value))
    return deadlines


def _deduplicate(deadlines: list[Deadline]) -> list[Deadline]:
    """Collapse the same obligation mentioned more than once.

    A renewal notice usually says the date twice — "renews on the 7th"
    and "renew by the 7th" — and those are one obligation, not two. The
    strongest claim wins: a deadline that is both a renewal and an expiry
    is one deadline.
    """
    best: dict[tuple[DeadlineKind, date], Deadline] = {}

    for deadline in deadlines:
        key = (deadline.kind, deadline.date)
        existing = best.get(key)
        if existing is None or deadline.confidence > existing.confidence:
            best[key] = deadline

    # The same date under two different kinds is still one obligation.
    by_date: dict[date, Deadline] = {}
    for (_kind, when), deadline in best.items():
        existing = by_date.get(when)
        if existing is None or deadline.confidence > existing.confidence:
            by_date[when] = deadline

    return list(by_date.values())


def _urgency_for(kind: DeadlineKind) -> int:
    """How many days ahead of the date to tell the user.

    A cancellation window is a deadline on the day the window *opens*,
    not the day it closes, so it needs a longer run-up than a payment.
    """
    return {
        DeadlineKind.CANCELLATION_WINDOW: 21,
        DeadlineKind.RENEWAL: 14,
        DeadlineKind.EXPIRY: 14,
        DeadlineKind.PAYMENT_DUE: 7,
        DeadlineKind.TRIAL_END: 3,
        DeadlineKind.NOTICE_PERIOD: 21,
        DeadlineKind.UNKNOWN: 7,
    }[kind]


def _title_for(kind: DeadlineKind, evidence: str) -> str:
    """A short label for the deadline, from the sentence it came from."""
    first_line = evidence.splitlines()[0].strip() if evidence else ""
    first_line = first_line.rstrip(".")
    if not first_line:
        return kind.value.replace("_", " ").capitalize()
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return first_line
