"""User-facing interfaces.

Phase 4 — Person B.

    bot/   platform-independent conversation (commands, briefings)
    chat/  Telegram long-polling transport
    web/   a loopback web view

Both talk to the same core: the agent pipeline in `agent/`, with the
household state in `agent/memory/hermes_memory.py`. No interface holds
logic of its own — chat and browser are two windows onto the same thing.
"""

__all__ = ["bot", "chat", "web"]
