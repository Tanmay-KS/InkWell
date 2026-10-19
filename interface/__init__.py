"""User-facing interfaces.

Phase 4 — Person B.

    bot/   Telegram / Slack handlers
    web/   a simple web view

Both talk to the same core: the agent pipeline in `agent/`, with the
household state in `agent/memory/hermes_memory.py`. Neither interface
holds logic of its own — the chat platform and the browser are two windows
onto the same thing.
"""

__all__ = ["bot", "web"]
