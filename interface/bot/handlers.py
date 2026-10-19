"""Chat platform handlers.

Phase 4 — Person B.

Hermes already speaks to Telegram, Discord, Slack, WhatsApp and twenty
other platforms through its messaging gateway. This module is the thin
Ledger-specific layer on top: how a renewal notice arriving in a chat
becomes a household item, and how a flagged deadline becomes a message.

The gateway is where personal details live in the clear, because the user's
own chat app is inside their boundary. That is fine and intended — the
boundary is the sandbox, and the chat app is the user's own device.

The flow:

    chat message  →  ingestion  →  memory  →  deadlines  →  action
         ↓                                                               ↓
    Hermes gateway (this module)  ←──────────  notification  ─────────┘

Phase 4 implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
        memory: The household memory store.
        allow_attachments: Whether inbound attachments are ingested. Off by
            default, because an attachment is untrusted input and the
            injection guard in `privacy_router/` is Phase 2 work that should
            land before the bot accepts documents from strangers.
    """

    def __init__(self, memory: object | None = None, *, allow_attachments: bool = False) -> None:
        self._memory = memory
        self.allow_attachments = allow_attachments

    def on_message(self, message: BotMessage) -> str:
        """Handle an inbound chat message and return the reply.

        Phase 4 implementation.
        """
        raise NotImplementedError("Phase 4 — see IMPLEMENTATION.md")

    def on_attachment(self, message: BotMessage) -> str:
        """Handle an inbound document.

        Phase 4 implementation.
        """
        raise NotImplementedError("Phase 4 — see IMPLEMENTATION.md")

    def daily_briefing(self, chat_id: str) -> str:
        """The proactive message: what is coming due.

        This is the message that makes Ledger worth having — it arrives
        without the user asking anything.

        Phase 4 implementation.
        """
        raise NotImplementedError("Phase 4 — see IMPLEMENTATION.md")
