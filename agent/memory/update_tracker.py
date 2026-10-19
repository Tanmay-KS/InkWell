"""Fact supersession — the "memory with updates" machinery.

Phase 3 — Person B.

Most agent memory demos test recall: *do you remember that I have a gym
membership?* That is the easy case, and it is what the Phase 0 plan
explicitly says is not enough.

The hard case is a fact that changed. The user cancels the plan in March.
The agent must now know the plan is cancelled — not believe both the old
fact and the new one, and not keep flagging a renewal for something that
was cancelled five months ago.

The model is simple and explicit:

    A fact has a value, a valid_from, and (once superseded) a valid_to.

Reads take the fact whose validity window contains "now". Writes that
contradict an existing fact close that fact's window and open a new one.
Nothing is ever deleted, so the history stays auditable — which is also
what lets the evaluation ask *when* did we learn this, not just *what* do
we believe.

The evaluation (`eval/memory_updates_eval.py`) builds questions of the form
"what is the status of X", where the answer depends on which fact is
current, and scores the agent against the expected current value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class FactKind(StrEnum):
    """The kinds of fact we track about the household."""

    SUBSCRIPTION_STATUS = "subscription_status"
    RENEWAL_DATE = "renewal_date"
    HOLDER = "holder"
    AMOUNT = "amount"
    CONTACT = "contact"
    PREFERENCE = "preference"


@dataclass(slots=True)
class Fact:
    """One version of one fact.

    Attributes:
        subject: What the fact is about — the item id, or a person.
        kind: Which kind of fact this is.
        value: The value. A string; structured kinds use a documented
            encoding so the table stays uniform.
        valid_from: When this version became true.
        valid_to: When it stopped being true, or None if it is current.
        source: Where we learned it.
        confidence: 0..1. A low-confidence fact is not allowed to close a
            high-confidence one without corroboration.
        metadata: Anything else.
    """

    subject: str
    kind: FactKind
    value: str
    valid_from: date
    valid_to: date | None = None
    source: str | None = None
    confidence: float = 1.0
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def is_current(self) -> bool:
        return self.valid_to is None

    def covers(self, when: date) -> bool:
        """Whether this fact was true at a given date."""
        if when < self.valid_from:
            return False
        return self.valid_to is None or when < self.valid_to


class UpdateTracker:
    """Applies fact updates and answers current-value queries.

    Phase 3 implementation: backed by the same SQLite sidecar as
    `hermes_memory.py`, in a `fact` table with a validity window.
    """

    def __init__(self, memory: object | None = None) -> None:
        self._memory = memory

    def record(self, fact: Fact) -> Fact:
        """Record a new version of a fact, superseding any open version.

        If an open fact exists for the same (subject, kind) and the new
        fact contradicts it, the old fact's `valid_to` is set to the day
        before the new fact's `valid_from`.

        A fact that *agrees* with the current value is not a new version —
        it is a restatement, and recording it as a new version would make
        the history noisy and the evaluation meaningless.
        """
        raise NotImplementedError("Phase 3 — see IMPLEMENTATION.md")

    def current(self, subject: str, kind: FactKind, *, when: date | None = None) -> Fact | None:
        """The fact that was true at a given date (default: today)."""
        raise NotImplementedError("Phase 3 — see IMPLEMENTATION.md")

    def history(self, subject: str, kind: FactKind) -> list[Fact]:
        """Every version of a fact, oldest first.

        The evaluation uses this to check not just that the agent believes
        the right thing now, but that the history is coherent — a fact that
        changed twice should have two prior versions with non-overlapping
        windows.
        """
        raise NotImplementedError("Phase 3 — see IMPLEMENTATION.md")
