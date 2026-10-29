"""Background deadline monitoring.

Phase 3 — Person A.

Entrypoint for a Nebius Serverless Job that runs on a schedule and asks
Ledger what is coming due.

The point of running this as a serverless job rather than a long-lived
process is that the check has to happen when nobody is looking. A renewal
notice that arrives on a Friday evening should be flagged on Saturday
morning without a human opening the chat app.

The job is a thin shell. It does not own the deadline logic — that lives
in `agent/deadlines/flag_deadlines.py` and `agent/memory/hermes_memory.py`.
It owns three things:

* **When** to run. Nebius Serverless Jobs are triggered by a schedule or
  by an event; the schedule is configured on the job, not in this file.
* **What to do with the result.** Anything due gets a notification through
  an injected notifier. The default notifier writes a safe event to the
  job log; a command notifier is the seam for Telegram/Slack.
* **Not doing it twice.** The handled log in `hermes_memory.py` is what
  makes the job idempotent — a job that runs every day must not re-notify
  about the same renewal every day.

The operational log deliberately does not contain holder names or account
references. The full notice is handed to the notifier, which is the only
part of this job allowed to talk to the user's private channel.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory, TrackedState
from jobs.notifier import (
    Notifier,
    command_notifier,
    stdout_notifier,
    telegram_notifier,
    webhook_notifier,
)

__all__ = ["JobConfig", "main", "run_once"]

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class JobConfig:
    """Configuration for the deadline monitor.

    Attributes:
        look_ahead_days: How far ahead to flag. A renewal a month out is
            not actionable yet; one inside a week is.
        notify: Whether to send notifications. False for a dry run.
        state_path: Where the job keeps its own run state, on the data
            volume. This is operational metadata, not the idempotency
            ledger; the ledger is the SQLite handled-event table.
        household_id: Which household to check. One household per
            deployment in the demo; the field exists so the multi-household
            version does not need a schema change.
        memory_path: SQLite sidecar path. An empty value lets
            `HouseholdMemory` use its normal environment default.
        notification_command: Optional executable that receives the full
            notice as JSON on stdin. When empty, the job uses the safe
            stdout notifier.
        notification_timeout_seconds: How long a command notifier may run
            before the attempt is treated as failed.
        notifier: Explicit notifier, mainly for tests and embedding.
        extra: Extension point. A callable `notifier` here is used when the
            field above is unset, so older call sites can inject one
            without changing the dataclass.
    """

    look_ahead_days: int = 14
    notify: bool = True
    state_path: str = field(
        default_factory=lambda: os.environ.get(
            "LEDGER_STATE_PATH", "/opt/data/state/job_state.json"
        )
    )
    household_id: str = field(
        default_factory=lambda: os.environ.get("LEDGER_HOUSEHOLD_ID", "default")
    )
    memory_path: str = field(
        default_factory=lambda: os.environ.get("LEDGER_MEMORY_DB", "")
    )
    notification_command: str = field(
        default_factory=lambda: os.environ.get("LEDGER_NOTIFY_COMMAND", "")
    )
    notification_timeout_seconds: float = 30.0
    notifier: Notifier | None = field(default=None, repr=False, compare=False)
    extra: dict[str, object] = field(default_factory=dict)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


def _notification(item: HouseholdItem, config: JobConfig, today: date) -> dict[str, Any]:
    """The full user-facing notice. It may contain personal detail."""
    days_until = (item.renewal_date - today).days if item.renewal_date else None
    return {
        "event": "deadline",
        "household_id": config.household_id,
        "item_id": item.item_id,
        "kind": item.kind,
        "provider": item.provider,
        "holder": item.holder,
        "reference": item.reference,
        "renewal_date": item.renewal_date.isoformat() if item.renewal_date else None,
        "days_until": days_until,
        "amount": item.amount,
    }


def _summary(item: HouseholdItem, today: date, *, notified: bool) -> dict[str, Any]:
    """The safe form returned to callers and written to job state.

    The notifier gets the full notice. This summary deliberately omits the
    holder and the reference so a caller can log it without becoming a
    second copy of the household picture.
    """
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "provider": item.provider,
        "renewal_date": item.renewal_date.isoformat() if item.renewal_date else None,
        "days_until": (item.renewal_date - today).days if item.renewal_date else None,
        "notified": notified,
    }


def _write_state(
    config: JobConfig,
    today: date,
    *,
    due: int,
    summaries: list[dict[str, Any]],
    notified: int,
    failures: int,
) -> None:
    """Write the run metadata atomically.

    The state file is diagnostic. If it cannot be written, the run still
    succeeds: the handled-event table is the source of truth for
    idempotency, and losing a diagnostic count must not lose a
    notification.
    """
    if not config.state_path:
        return

    path = Path(config.state_path)
    state = {
        "last_run": datetime.now(UTC).isoformat(),
        "run_date": today.isoformat(),
        "household_id": config.household_id,
        "look_ahead_days": config.look_ahead_days,
        "due": due,
        "flagged": len(summaries),
        "notified": notified,
        "failures": failures,
    }

    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        LOG.warning("could not write job state (%s)", type(exc).__name__)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def run_once(
    config: JobConfig | None = None,
    *,
    today: date | None = None,
    memory: HouseholdMemory | None = None,
    notifier: Notifier | None = None,
) -> list[dict[str, Any]]:
    """Run one pass of the deadline check.

    Args:
        config: Job configuration. Defaults to environment-driven values.
        today: The reference date. Defaults to the local date.
        memory: An existing memory store, mainly for tests. When omitted,
            the job opens one itself and closes it before returning.
        notifier: Explicit notifier. Overrides the config field and the
            callable in `config.extra`.

    Returns:
        One safe summary per item flagged in this run. Items with a
        previous `notified` event are not returned, which is what makes a
        second run quiet. In dry-run mode the summary is returned with
        `notified=False`.

    Raises:
        ValueError: The look-ahead window is negative.
        Exception: A memory/query failure propagates; the job should fail
            rather than silently report a clean run.
    """
    config = config or JobConfig()
    if config.look_ahead_days < 0:
        raise ValueError("look_ahead_days must be non-negative")

    when = today or date.today()
    owns_memory = memory is None
    if memory is None:
        memory = (
            HouseholdMemory(Path(config.memory_path))
            if config.memory_path
            else HouseholdMemory()
        )

    try:
        due = memory.due_within(config.look_ahead_days, today=when)

        send = notifier or config.notifier
        if send is None:
            extra_notifier = config.extra.get("notifier")
            if callable(extra_notifier):
                send = extra_notifier
        if send is None and config.notify:
            if config.notification_command:
                send = command_notifier(
                    config.notification_command,
                    timeout=config.notification_timeout_seconds,
                )
            elif os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
                send = telegram_notifier()
            elif os.environ.get("LEDGER_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL"):
                send = webhook_notifier()
            else:
                send = stdout_notifier()

        summaries: list[dict[str, Any]] = []
        notified_count = 0
        failures = 0

        for item in due:
            # `due_within` already excludes dismissed items. A handled item
            # is also finished, even if it has no notification event.
            if item.state is TrackedState.HANDLED:
                continue
            if memory.was_handled(item.item_id, "notified"):
                continue

            notified = False
            if config.notify:
                if send is None:
                    raise RuntimeError("notify is enabled but no notifier is configured")
                try:
                    send(_notification(item, config, when))
                except Exception as exc:
                    # The exception message is deliberately not logged: a
                    # notifier is third-party code and its message is
                    # unbounded text.
                    failures += 1
                    LOG.warning(
                        "notification failed for %s (%s)",
                        item.item_id,
                        type(exc).__name__,
                    )
                else:
                    try:
                        memory.record_event(
                            item.item_id,
                            "notified",
                            detail=f"look_ahead_days={config.look_ahead_days}",
                        )
                    except Exception as exc:
                        failures += 1
                        LOG.warning(
                            "could not record notification for %s (%s)",
                            item.item_id,
                            type(exc).__name__,
                        )
                    else:
                        notified = True
                        notified_count += 1

            summaries.append(_summary(item, when, notified=notified))

        _write_state(
            config,
            when,
            due=len(due),
            summaries=summaries,
            notified=notified_count,
            failures=failures,
        )
        LOG.info(
            "deadline monitor complete: due=%d flagged=%d notified=%d failures=%d",
            len(due),
            len(summaries),
            notified_count,
            failures,
        )
        return summaries
    finally:
        if owns_memory:
            memory.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--look-ahead-days",
        type=int,
        default=None,
        help="override LEDGER_LOOK_AHEAD_DAYS",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="reference date in YYYY-MM-DD form (for testing and backfills)",
    )
    parser.add_argument("--memory-db", default=None, help="SQLite sidecar path")
    parser.add_argument("--state-path", default=None, help="job state JSON path")
    parser.add_argument(
        "--notify-command",
        default=None,
        help="executable that receives the full notice as JSON on stdin",
    )
    parser.add_argument(
        "--notify-timeout",
        type=float,
        default=None,
        help="maximum seconds for one command-notifier attempt",
    )
    parser.add_argument(
        "--notify-telegram",
        action="store_true",
        help="send notifications via Telegram bot",
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=None,
        help="Telegram bot token (default: $TELEGRAM_BOT_TOKEN)",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=None,
        help="Telegram chat ID (default: $TELEGRAM_CHAT_ID)",
    )
    parser.add_argument(
        "--webhook-url",
        default=None,
        help="Webhook URL (default: $LEDGER_WEBHOOK_URL or $SLACK_WEBHOOK_URL)",
    )
    notify = parser.add_mutually_exclusive_group()
    notify.add_argument(
        "--notify",
        dest="notify",
        action="store_true",
        default=None,
        help="send notifications (the default unless --dry-run)",
    )
    notify.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        default=None,
        help="do not send notifications",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="query and report without notifying or marking items handled",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the Nebius Serverless Job container.

    Reads configuration from the environment (with CLI overrides for local
    dry runs), runs one pass, and exits non-zero if a notification failed.
    The job's schedule is configured on the Nebius side, not here.
    """
    args = _parse_args(argv)

    try:
        config = JobConfig(
            look_ahead_days=(
                args.look_ahead_days
                if args.look_ahead_days is not None
                else _env_int("LEDGER_LOOK_AHEAD_DAYS", 14)
            ),
            notify=(
                False
                if args.dry_run
                else (
                    args.notify
                    if args.notify is not None
                    else _env_bool("LEDGER_NOTIFY", True)
                )
            ),
            state_path=(
                args.state_path
                if args.state_path is not None
                else os.environ.get(
                    "LEDGER_STATE_PATH", "/opt/data/state/job_state.json"
                )
            ),
            household_id=os.environ.get("LEDGER_HOUSEHOLD_ID", "default"),
            memory_path=(
                args.memory_db
                if args.memory_db is not None
                else os.environ.get("LEDGER_MEMORY_DB", "")
            ),
            notification_command=(
                args.notify_command
                if args.notify_command is not None
                else os.environ.get("LEDGER_NOTIFY_COMMAND", "")
            ),
            notification_timeout_seconds=(
                args.notify_timeout
                if args.notify_timeout is not None
                else _env_float("LEDGER_NOTIFY_TIMEOUT_SECONDS", 30.0)
            ),
        )
    except ValueError as exc:
        print(f"ledger: invalid configuration ({exc})", file=sys.stderr)
        return 1

    notifier: Notifier | None = None
    if config.notify:
        try:
            if config.notification_command:
                notifier = command_notifier(
                    config.notification_command,
                    timeout=config.notification_timeout_seconds,
                )
            elif (
                args.notify_telegram
                or os.environ.get("LEDGER_NOTIFY_CHANNEL") == "telegram"
                or (
                    (args.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN"))
                    and (args.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID"))
                )
            ):
                notifier = telegram_notifier(
                    bot_token=args.telegram_bot_token,
                    chat_id=args.telegram_chat_id,
                )
            elif (
                args.webhook_url
                or os.environ.get("LEDGER_WEBHOOK_URL")
                or os.environ.get("SLACK_WEBHOOK_URL")
            ):
                notifier = webhook_notifier(url=args.webhook_url)
            else:
                notifier = stdout_notifier()
        except Exception as exc:
            print(
                f"ledger: invalid notifier configuration ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 1

    try:
        flagged = run_once(config, today=args.today, notifier=notifier)
    except Exception as exc:
        print(
            f"ledger: deadline monitor failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1

    print(f"ledger: flagged {len(flagged)} item(s) for {config.household_id}")
    failures = sum(
        1 for item in flagged if config.notify and not item.get("notified", False)
    )
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
