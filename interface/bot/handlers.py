"""Chat platform handlers.

Phase 4 — Person B.

Hermes already speaks to Telegram, Discord, Slack, WhatsApp and twenty
other platforms through its messaging gateway. This module is the thin
Ledger-specific layer on top: how a renewal notice arriving in a chat
becomes a household item, and how a flagged deadline becomes a message.

Chat is an external destination. Status messages contain only the provider
and date; draft previews run through the local redactor. The full draft is
shown only in the loopback web interface.

The flow:

    chat message  →  ingestion  →  memory  →  deadlines  →  action
         ↓                                                               ↓
    Hermes gateway (this module)  ←──────────  notification  ─────────┘
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from agent.pipeline import process_document

__all__ = ["BotMessage", "LedgerBot"]


@dataclass(slots=True)
class BotMessage:
    """A message in or out of the chat."""

    text: str
    chat_id: str
    user_id: str | None = None
    message_id: str | None = None
    attachments: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class LedgerBot:
    """Ledger's behaviour inside a chat platform.

    Attributes:
        allow_attachments: Whether inbound attachments are ingested. Their
            contents are treated as untrusted text, never as instructions.
    """

    def __init__(self, memory: object | None = None, *, allow_attachments: bool = True) -> None:
        self._memory = memory
        self.allow_attachments = allow_attachments

    def on_message(self, message: BotMessage) -> str:
        """Handle an inbound chat message and return the reply.

        Answers local questions about tracked items and upcoming dates.
        Explicit cancellation/reactivation requests become dated status facts;
        unsupported requests are answered with the available capabilities.
        """
        if self._memory is None:
            return "Ledger memory is not configured."
        command = message.text.strip()
        lowered = command.casefold()
        if lowered in {"/help", "/start", "help"}:
            return "Commands: /due, /cancel ITEM_ID [YYYY-MM-DD], /active ITEM_ID [YYYY-MM-DD]."
        if lowered in {"/due", "due"}:
            return self.daily_briefing(message.chat_id)
        # Slash commands are parsed first. The old order let the generic
        # "cancel" keyword swallow `/cancel item-…`, which then matched the
        # item lookup and answered with its current status instead of
        # recording the change.
        if lowered.startswith("/"):
            reply = self._apply_status_request(command, message)
            if reply is not None:
                return reply
            return (
                "I don't know that command. Commands: /due, "
                "/cancel ITEM_ID [YYYY-MM-DD], /active ITEM_ID [YYYY-MM-DD]."
            )
        if lowered in {"hi", "hello", "hey", "what can you do?"}:
            return (
                "I can show upcoming dates, list tracked items, answer when an "
                "item renews, and record a cancellation or reactivation. "
                "Try ‘What’s due?’ or ‘When does Orchard Fitness renew?’"
            )
        if any(term in lowered for term in ("due", "coming up", "upcoming", "renewals")):
            return self.daily_briefing(message.chat_id)
        if any(term in lowered for term in ("list", "show my items", "what do i have", "household")):
            items = self._memory.all_items()
            if not items:
                return "There are no tracked items yet. Upload a notice to get started."
            return "Tracked items:\n" + "\n".join(
                f"• {item.provider} ({item.kind}) — {item.renewal_date.isoformat() if item.renewal_date else 'no date'}; {self._status(item)}"
                for item in items
            )
        if any(word in lowered for word in ("cancel", "cancelled", "canceled", "reactivate", "active again")):
            reply = self._apply_status_request(command, message)
            if reply is not None:
                return reply
        candidates = self._matching_items(lowered)
        if len(candidates) == 1:
            item = candidates[0]
            renewal = item.renewal_date.isoformat() if item.renewal_date else "no renewal date on file"
            return f"{item.provider} ({item.kind}) is {self._status(item)} and renews {renewal}."
        if len(candidates) > 1:
            return "I found more than one match. Please name the provider or item ID.\n" + "\n".join(
                f"• {item.provider}: {item.item_id}" for item in candidates
            )
        return "I can help with upcoming dates, tracked items, renewal dates, and cancellation status. Try ‘help’ to see examples."

    def _status(self, item) -> str:
        from agent.memory.update_tracker import FactKind, UpdateTracker

        fact = UpdateTracker(self._memory).current(item.item_id, FactKind.SUBSCRIPTION_STATUS)
        if fact:
            return fact.value
        # `TrackedState` is the item's lifecycle, not its subscription
        # status. A freshly imported item has not been cancelled, so it is
        # active — showing "new" in a chat reply reads like an error.
        return "dismissed" if item.state.value == "dismissed" else "active"

    def _matching_items(self, text: str) -> list:
        items = self._memory.all_items()
        exact_id = [item for item in items if item.item_id.casefold() in text]
        if exact_id:
            return exact_id
        return [
            item for item in items
            if item.provider.casefold() in text or item.kind.casefold() in text
        ]

    def _apply_status_request(self, text: str, message: BotMessage) -> str | None:
        lowered = text.casefold().strip().rstrip(".!?")
        if lowered.startswith("/"):
            lowered = lowered[1:].lstrip()

        effective: date | None = None
        date_match = re.search(r"\s+(\d{4}-\d{2}-\d{2})$", lowered)
        if date_match:
            try:
                effective = date.fromisoformat(date_match.group(1))
            except ValueError:
                return "Use a valid date in YYYY-MM-DD format."
            lowered = lowered[: date_match.start()]

        patterns = (
            (r"^(?:please\s+)?cancel(?:\s+the)?\s+(.+)$", "cancelled"),
            (r"^i\s+(?:have\s+)?cancelled\s+(?:the\s+)?(.+)$", "cancelled"),
            (r"^i\s+(?:have\s+)?canceled\s+(?:the\s+)?(.+)$", "cancelled"),
            (r"^mark\s+(.+?)\s+(?:as\s+)?cancelled$", "cancelled"),
            (r"^mark\s+(.+?)\s+(?:as\s+)?canceled$", "cancelled"),
            (r"^(?:please\s+)?(?:reactivate|activate|active)\s+(?:the\s+)?(.+)$", "active"),
            (r"^mark\s+(.+?)\s+(?:as\s+)?active$", "active"),
        )
        intent = None
        for pattern, status in patterns:
            match = re.fullmatch(pattern, lowered)
            if match:
                intent = (match.group(1), status)
                break
        if intent is None:
            return None
        target, status = intent
        candidates = self._matching_items(target)
        if len(candidates) != 1:
            if candidates:
                return "Which item should I update? Use its provider name or item ID:\n" + "\n".join(
                    f"• {item.provider}: {item.item_id}" for item in candidates
                )
            return "I couldn't match that request to a tracked item, so nothing changed."
        item = candidates[0]
        when = effective or date.today()
        self._memory.record_subscription_status(
            item.item_id, status, effective_date=when, source=message.message_id or "web chat"
        )
        return (
            f"Recorded {item.provider} as {status} from {when.isoformat()}. "
            f"It will {'no longer appear' if status == 'cancelled' else 'appear'} "
            "in upcoming reminders as appropriate."
        )

    def on_attachment(self, message: BotMessage) -> str:
        """Handle an inbound document.

        Imports allowed local attachments through the document pipeline. The
        bot returns a redacted draft preview; the full draft is available in
        the local web view.
        """
        if not self.allow_attachments:
            return "Document uploads are disabled."
        if self._memory is None:
            return "Ledger memory is not configured."
        replies = []
        for attachment in message.attachments:
            path = Path(attachment)
            if not path.is_file():
                replies.append("An attachment could not be opened.")
                continue
            result = process_document(path, memory=self._memory)
            if not result.ok:
                replies.append("I could not find a dated obligation in that file.")
                continue
            replies.append(
                f"Tracked {result.item.provider}; next date {result.action.metadata['deadline_date']}.\n\n"
                f"Draft preview (personal details masked):\n{result.action.redacted_body()}"
            )
        return "\n\n".join(replies) if replies else "No document was attached."

    def daily_briefing(self, chat_id: str) -> str:
        """The proactive message: what is coming due.

        This is the message that makes Ledger worth having — it arrives
        without the user asking anything.

        Lists upcoming items from the household sidecar.
        """
        if self._memory is None:
            return "Ledger memory is not configured."
        due = self._memory.due_within(30)
        if not due:
            return "Nothing is due in the next 30 days."
        lines = ["Coming up in the next 30 days:"]
        for item in due:
            lines.append(f"• {item.provider} — {item.renewal_date.isoformat()}")
        return "\n".join(lines)
