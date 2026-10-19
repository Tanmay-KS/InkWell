"""Deadline detection.

Phase 1 — Person B.

Turns extracted document text into dated obligations. This is where the
plan's "document in → deadline flagged" arrow actually lives.

The output is not a notification. It is a `Deadline` record: a claim that
this document contains an obligation, with the evidence for it. The
evidence matters — when the user asks "why are you telling me this?", the
agent has to point at the line in the document that said it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


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


def flag_deadlines(
    text: str,
    *,
    today: date | None = None,
    model: str | None = None,
) -> list[Deadline]:
    """Find the dated obligations in a document.

    Args:
        text: The extracted document text.
        today: The reference date, for "in 30 days" style language and for
            deciding which deadlines are already past.
        model: The model to use. Defaults to `LEDGER_MODEL_SUPER` — this
            stage needs real reasoning, not the cheap tier.

    Returns:
        Deadlines sorted by date, soonest first. Past deadlines are
        included but flagged, because a document that arrived late may
        still contain an obligation the user has to act on.

    Phase 1 implementation: ask the model for a structured extraction, then
    validate every returned date against the source text before accepting
    it. A date the model produced but the text does not contain is a
    hallucination and is dropped, not silently kept.
    """
    raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")


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
    raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")
