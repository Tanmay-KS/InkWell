"""Telegram adapter: long polling into `LedgerBot`.

`interface/bot/handlers.py` is the platform-independent conversation.
This package is transport: it turns a Telegram update into a `BotMessage`,
hands it to `LedgerBot`, and posts the reply back.

The adapter does not send outbound actions. A reply answers a question; a
drafted letter is only ever shown for the user to send.
"""

from __future__ import annotations

__all__ = ["telegram"]
