"""Draft action generation.

Phase 1 — Person B.

Turns a deadline into something the user can act on. The plan's first
draft actions are a cancellation email and a refund request.

Two rules shape this module:

* **A draft, not an action.** Ledger never sends anything. It produces
  text the user reads, edits, and sends. This is not only a safety
  property — it is what makes the agent trustworthy enough to hold a
  household's renewal dates.
* **Personal details are placeholders in the draft, not the real thing.**
  A draft written in the user's own voice contains their policy number,
  their address, their account. That draft is generated inside the
  sandbox, and the version that leaves is the redacted one — the personal
  details are substituted back in only at the moment of display, in the
  user's own channel, which is inside the sandbox boundary.

  This is the subtle part of the whole project: the agent genuinely needs
  the real values to write a useful draft, and yet the values must not
  travel. See `docs/architecture.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ActionKind(StrEnum):
    """The kinds of action Ledger knows how to draft."""

    CANCEL = "cancel"
    REFUND = "refund"
    RENEW = "renew"
    REMIND = "remind"
    CONTACT = "contact"
    DISPUTE = "dispute"


@dataclass(slots=True)
class DraftAction:
    """A drafted action for the user to review and send.

    Attributes:
        kind: What kind of action this is.
        subject: The household item it concerns.
        title: A short label for the UI.
        body: The draft text, with personal details still in place — it is
            generated and consumed inside the sandbox.
        channel: Where it would be sent, if sent.
        recipient: Who it would go to.
        personal_details: The values referenced in the body, so that the
            redaction step knows what to mask before the draft is shown
            outside the boundary.
        metadata: Everything else.
    """

    kind: ActionKind
    subject: str | None
    title: str
    body: str
    channel: str | None = None
    recipient: str | None = None
    personal_details: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


def draft_action(
    deadline: object,
    *,
    context: object | None = None,
    model: str | None = None,
) -> DraftAction:
    """Draft an action for a deadline.

    Args:
        deadline: The `Deadline` to act on.
        context: The household item and any prior related correspondence.
        model: Defaults to `LEDGER_MODEL_SUPER`.

    Returns:
        A `DraftAction`. The body is written to be sent as-is by a human:
        no placeholders like [YOUR_NAME], no meta-commentary about being
        an AI, and a specific ask with a date attached.

    Phase 1 implementation: two variants — cancellation and refund — with
    a generic reminder as the third.
    """
    raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")
