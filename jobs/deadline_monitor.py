"""Background deadline monitoring.

Phase 3 — Person A.

Entrypoint for a Nebius Serverless Job that runs on a schedule and asks
InkWell what is coming due.

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
  the messaging gateway, and anything already handled is left alone.
* **Not doing it twice.** The handled log in `hermes_memory.py` is what
  makes the job idempotent — a job that runs every day must not re-notify
  about the same renewal every day.

Two properties are deliberate:

* **The run record is the liveness evidence.** Each real run writes
  `last_run` to `state_path`. A schedule that exists but never fires looks
  identical to a working one from the console; this file is how
  `--status` tells them apart.
* **The notification carries no personal detail.** A serverless job's
  stdout is a log. The provider and the date are enough to act on; the
  holder and the account number are not, and they stay out of the record.

Usage:

    python -m jobs.deadline_monitor                 # one pass, as the job does
    python -m jobs.deadline_monitor --dry-run       # report, do not notify, do not record
    python -m jobs.deadline_monitor --status        # is the schedule actually firing?
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from agent.memory.hermes_memory import SIDECAR_PATH, HouseholdItem, HouseholdMemory

try:
    from agent.verifier import inventory_findings
except ImportError:
    inventory_findings = None  # Minimal container image does not stage verifier

from jobs.notifier import (
    command_notifier,
    stdout_notifier,
    telegram_notifier,
    webhook_notifier,
)

__all__ = ["JobConfig", "main", "run_once", "state_status"]

Notifier = Callable[[dict[str, Any]], None]

EVENT_PREFIX = "deadline_notified"
LOG = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        val = float(raw)
        return val if val > 0 else default
    except ValueError:
        return default


@dataclass(slots=True)
class JobConfig:
    """Configuration for the deadline monitor.

    Attributes:
        look_ahead_days: How far ahead to flag. A renewal a month out is
            not actionable yet; one inside a week is.
        notify: Whether to send notifications. False for a dry run.
        state_path: Where the job keeps its own run state, on the data
            volume. This is the liveness record, not the memory database.
        household_id: Which household to check. One household per
            deployment in the demo; the field exists so the multi-household
            version does not need a schema change.
        memory_path: The SQLite sidecar. Empty means the standard location
            (`INKWELL_MEMORY_DB`, else the Hermes data volume).
        notification_command: Optional executable that receives the notice
            as JSON on stdin.
        notification_timeout_seconds: Timeout for command notifier execution.
        notifier: Explicit notifier instance.
        extra: Additional configuration dictionary.
    """

    look_ahead_days: int = 14
    notify: bool = True
    state_path: str = field(
        default_factory=lambda: os.environ.get(
            "INKWELL_STATE_PATH", "/opt/data/state/job_state.json"
        )
    )
    household_id: str = field(
        default_factory=lambda: os.environ.get("INKWELL_HOUSEHOLD_ID", "default")
    )
    memory_path: str = field(
        default_factory=lambda: os.environ.get("INKWELL_MEMORY_DB", "")
    )
    notification_command: str = field(
        default_factory=lambda: os.environ.get("INKWELL_NOTIFY_COMMAND", "")
    )
    notification_timeout_seconds: float = field(
        default_factory=lambda: _env_float("INKWELL_NOTIFY_TIMEOUT_SECONDS", 30.0)
    )
    notifier: Notifier | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _event_for(item: HouseholdItem) -> str:
    """The handled-log key for "we told the user about this renewal"."""
    when = item.renewal_date.isoformat() if item.renewal_date else "unknown"
    return f"{EVENT_PREFIX}:{when}"


def _record(item: HouseholdItem, today: date) -> dict[str, Any]:
    """The notification payload.

    The holder and the reference are absent on purpose: this record is
    written to a serverless log, and a log is not a place for an account
    number.
    """
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "provider": item.provider,
        "renewal_date": item.renewal_date.isoformat() if item.renewal_date else None,
        "days_until": (item.renewal_date - today).days if item.renewal_date else None,
        "amount": item.amount,
    }


def _log_notification(record: dict[str, Any]) -> None:
    """The default notifier: one line on stdout."""
    stdout_notifier()(record)


def _write_state(
    config: JobConfig,
    *,
    today: date,
    checked: int,
    flagged: list[dict[str, Any]],
    findings: dict[str, Any] | None = None,
) -> Path:
    """Record that this run happened atomically."""
    path = Path(config.state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        if not os.environ.get("INKWELL_STATE_PATH"):
            path = Path("/tmp/inkwell_job_state.json")
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            raise
    payload = {
        "last_run": datetime.now(UTC).isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "household_id": config.household_id,
        "look_ahead_days": config.look_ahead_days,
        "notify": config.notify,
        "checked": checked,
        "flagged": [
            {"item_id": record["item_id"], "renewal_date": record.get("renewal_date")}
            for record in flagged
            if "item_id" in record
        ],
        "findings": findings or {},
        "findings_summary": (findings or {}).get("summary", ""),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def run_once(
    config: JobConfig | None = None,
    *,
    today: date | None = None,
    memory: HouseholdMemory | None = None,
    notifier: Notifier | None = None,
) -> list[dict[str, Any]]:
    """Run one pass of the deadline check."""
    config = config or JobConfig()
    today = today or datetime.now(UTC).date()
    notify = config.notify

    deliver = notifier or config.notifier
    if deliver is None and notify:
        if config.notification_command:
            deliver = command_notifier(
                config.notification_command,
                timeout=config.notification_timeout_seconds,
            )
        elif os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
            deliver = telegram_notifier()
        elif os.environ.get("INKWELL_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL"):
            deliver = webhook_notifier()
        else:
            deliver = _log_notification
    elif deliver is None:
        deliver = _log_notification

    owns_memory = memory is None
    if memory is None:
        memory = HouseholdMemory(
            Path(config.memory_path) if config.memory_path else SIDECAR_PATH
        )
        memory.connect()

    flagged: list[dict[str, Any]] = []
    findings: dict[str, Any] = {}
    try:
        items = memory.due_within(config.look_ahead_days, today=today)
        if callable(inventory_findings):
            all_items = memory.all_items()
            findings = inventory_findings(
                all_items, today=today, horizon_days=config.look_ahead_days
            )
        for item in items:
            event = _event_for(item)
            if memory.was_handled(item.item_id, event):
                continue

            record = _record(item, today)
            if notify:
                try:
                    deliver(record)
                except Exception as exc:
                    record["notified"] = False
                    record["error"] = str(exc)
                    flagged.append(record)
                    continue
                memory.record_event(item.item_id, event, json.dumps(record, sort_keys=True))
                record["notified"] = True
            else:
                record["notified"] = False

            flagged.append(record)

        duplicates = findings.get("duplicates") or []
        if isinstance(duplicates, list) and duplicates:
            first = duplicates[0].get("first") if isinstance(duplicates[0], dict) else None
            owner_id = str(first.get("item_id") or "") if isinstance(first, dict) else ""
            event = f"{EVENT_PREFIX}:verifier:{today.isoformat()}"
            if owner_id and not memory.was_handled(owner_id, event):
                summary_record: dict[str, Any] = {
                    "item_id": owner_id,
                    "kind": "findings",
                    "provider": "InkWell",
                    "summary": findings.get("summary", ""),
                    "due_count": len(findings.get("due") or []),
                    "duplicate_count": len(duplicates),
                }
                if notify:
                    try:
                        deliver(summary_record)
                        memory.record_event(
                            owner_id, event, json.dumps(summary_record, sort_keys=True)
                        )
                        summary_record["notified"] = True
                    except Exception as exc:
                        summary_record["notified"] = False
                        summary_record["error"] = str(exc)
                else:
                    summary_record["notified"] = False
                flagged.append(summary_record)
    finally:
        if owns_memory:
            memory.close()

    if notify:
        _write_state(
            config, today=today, checked=len(items), flagged=flagged, findings=findings
        )

    return flagged


def state_status(state_path: str | Path, *, max_age_hours: float = 36.0) -> tuple[int, str]:
    """Is the schedule actually firing?"""
    path = Path(state_path)
    if not path.exists():
        return 1, f"no run record at {path} — the job has never run here"

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        last_run = datetime.fromisoformat(str(payload["last_run"]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return 1, f"run record at {path} is unreadable: {exc}"

    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=UTC)
    age = datetime.now(UTC) - last_run
    age_hours = age.total_seconds() / 3600

    if age_hours > max_age_hours:
        return 1, (
            f"last run {last_run.isoformat()} is {age_hours:.1f}h old "
            f"(limit {max_age_hours:.0f}h) — the schedule is not firing"
        )
    return 0, (
        f"last run {last_run.isoformat()} ({age_hours:.1f}h ago), "
        f"{payload.get('checked', 0)} checked, "
        f"{len(payload.get('flagged', []))} flagged"
    )


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the Nebius Serverless Job container."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report, do not notify")
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="reference date for due calculations (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--look-ahead",
        "--look-ahead-days",
        dest="look_ahead_days",
        type=int,
        default=None,
        help="days to look ahead",
    )
    parser.add_argument(
        "--state",
        "--state-path",
        dest="state_path",
        default=None,
        help="run-record path",
    )
    parser.add_argument(
        "--db",
        "--memory-db",
        dest="memory_db",
        default=None,
        help="memory database path",
    )
    parser.add_argument(
        "--household",
        "--household-id",
        dest="household_id",
        default=None,
        help="household id",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="check the run record instead of running; exits 1 when stale",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=36.0,
        help="staleness limit for --status",
    )
    parser.add_argument(
        "--notify-command",
        default=None,
        help="executable command receiving notice JSON on stdin",
    )
    parser.add_argument(
        "--notify-timeout",
        type=float,
        default=None,
        help="maximum seconds for command notifier",
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
        help="Webhook URL (default: $INKWELL_WEBHOOK_URL or $SLACK_WEBHOOK_URL)",
    )

    args = parser.parse_args(argv)

    config = JobConfig()
    if args.state_path:
        config.state_path = args.state_path
    if args.memory_db:
        config.memory_path = args.memory_db
    if args.household_id:
        config.household_id = args.household_id
    if args.look_ahead_days is not None:
        config.look_ahead_days = args.look_ahead_days
    if args.notify_command is not None:
        config.notification_command = args.notify_command
    if args.notify_timeout is not None:
        config.notification_timeout_seconds = args.notify_timeout

    if args.status:
        code, message = state_status(config.state_path, max_age_hours=args.max_age_hours)
        print(f"inkwell: {message}")
        return code

    if args.dry_run:
        config.notify = False

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
                or os.environ.get("INKWELL_NOTIFY_CHANNEL") == "telegram"
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
                or os.environ.get("INKWELL_WEBHOOK_URL")
                or os.environ.get("SLACK_WEBHOOK_URL")
            ):
                notifier = webhook_notifier(url=args.webhook_url)
        except Exception as exc:
            print(
                f"inkwell: invalid notifier configuration ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 1

    today = args.today or datetime.now(UTC).date()
    try:
        flagged = run_once(config, today=today, notifier=notifier)
    except OSError as exc:
        print(f"inkwell: could not record the run: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"inkwell: deadline monitor failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1

    print(f"inkwell: flagged {len(flagged)} item(s) for {config.household_id}")
    if not config.notify:
        print("inkwell: dry run — nothing notified, nothing recorded")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
