"""Run the Telegram adapter.

    python -m interface.chat
    make telegram-bot

The adapter shares `LedgerBot` and the household database with the web
view, so a message in chat and a click in the browser see the same state.
Credentials come from `.env` or the environment:

    TELEGRAM_BOT_TOKEN     required, from @BotFather
    TELEGRAM_CHAT_ID       optional, for proactive notices only

Long polling is used, so no public URL or webhook is needed.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from agent.memory.hermes_memory import HouseholdMemory
from interface.bot.handlers import LedgerBot
from interface.chat.telegram import TelegramAPI, TelegramBot, TelegramError

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m interface.chat", description=__doc__)
    parser.add_argument(
        "--memory",
        default="",
        help="household database path (default: $LEDGER_MEMORY_DB or the sidecar)",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=25,
        help="long-poll seconds (default: 25)",
    )
    parser.add_argument(
        "--no-attachments",
        action="store_true",
        help="ignore inbound documents",
    )
    args = parser.parse_args(argv)

    # `.env` is the project's credential file. `load_dotenv` does not
    # override an explicitly exported variable, so a shell environment
    # still wins over the file.
    load_dotenv(override=False)

    memory = HouseholdMemory(args.memory or None)
    memory.connect()
    bot = LedgerBot(memory, allow_attachments=not args.no_attachments)

    try:
        adapter = TelegramBot(bot, TelegramAPI())
        print("ledger: Telegram bot polling; press Ctrl-C to stop", flush=True)
        adapter.run_forever(timeout=args.poll_timeout)
    except (TelegramError, ValueError) as exc:
        print(f"ledger: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        memory.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
