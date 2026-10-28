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
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from agent.memory.hermes_memory import SIDECAR_PATH, HouseholdItem, HouseholdMemory

__all__ = ["JobConfig", "main", "run_once", "state_status"]

Notifier = Callable[[dict[str, object]], None]

EVENT_PREFIX = "deadline_notified"


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
            (`LEDGER_MEMORY_DB`, else the Hermes data volume).
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
    memory_path: str = field(default_factory=lambda: os.environ.get("LEDGER_MEMORY_DB", ""))
    extra: dict[str, object] = field(default_factory=dict)


def _event_for(item: HouseholdItem) -> str:
    """The handled-log key for "we told the user about this renewal".

    The date is part of the key. A renewal that moves — the provider
    re-issues the notice with a new date — is a new thing to say, and a
    key without the date would swallow it.
    """
    when = item.renewal_date.isoformat() if item.renewal_date else "unknown"
    return f"{EVENT_PREFIX}:{when}"


def _record(item: HouseholdItem, today: date) -> dict[str, object]:
    """The notification payload.

    The holder and the reference are absent on purpose: this record is
    written to a serverless log, and a log is not a place for an account
    number. The item id is enough for the gateway to look up the rest.
    """
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "provider": item.provider,
        "renewal_date": item.renewal_date.isoformat() if item.renewal_date else None,
        "days_until": (item.renewal_date - today).days if item.renewal_date else None,
        "amount": item.amount,
    }


def _log_notification(record: dict[str, object]) -> None:
    """The default notifier: one line on stdout.

    On the serverless job this lands in the job log. The messaging gateway
    is wired in by passing its sender as `notifier` — see the module
    docstring — and that sender is the thing that talks to the user.
    """
    when = record.get("renewal_date") or "an unknown date"
    days = record.get("days_until")
    eta = f" ({days} day(s))" if isinstance(days, int) else ""
    print(f"ledger: due {when}{eta} — {record.get('provider')} {record.get('kind')}")


def _write_state(config: JobConfig, *, today: date, checked: int, flagged: list[dict]) -> Path:
    """Record that this run happened.

    Written atomically: a job killed mid-write must leave either the
    previous record or the new one, never a truncated file that `--status`
    reads as corrupt.
    """
    path = Path(config.state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_run": datetime.now(UTC).isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "household_id": config.household_id,
        "look_ahead_days": config.look_ahead_days,
        "notify": config.notify,
        "checked": checked,
        "flagged": [
            {"item_id": record["item_id"], "renewal_date": record["renewal_date"]}
            for record in flagged
        ],
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
) -> list[dict[str, object]]:
    """Run one pass of the deadline check.

    Args:
        config: Job configuration. Defaults to one built from the
            environment.
        today: The reference date. Defaults to the real date; tests pass
            a fixed one.
        memory: An open memory store. When supplied it is not closed; the
            caller owns it. Otherwise one is opened at `config.memory_path`
            and closed before returning.
        notifier: Where a due item goes. Defaults to a stdout line; the
            messaging gateway passes its sender here.

    Returns:
        The items that were flagged, as plain dicts. Empty when there is
        nothing due, or when everything due was already announced.

    Raises:
        OSError: if the run record cannot be written. A job that cannot
            prove it ran is not a job that ran, and swallowing this would
            make the liveness check meaningless.
    """
    config = config or JobConfig()
    today = today or datetime.now(UTC).date()
    notify = config.notify
    deliver = notifier or _log_notification

    owns_memory = memory is None
    if memory is None:
        memory = HouseholdMemory(
            Path(config.memory_path) if config.memory_path else SIDECAR_PATH
        )
        memory.connect()

    flagged: list[dict[str, object]] = []
    try:
        items = memory.due_within(config.look_ahead_days, today=today)
        for item in items:
            event = _event_for(item)
            if memory.was_handled(item.item_id, event):
                continue

            record = _record(item, today)
            if notify:
                try:
                    deliver(record)
                except Exception as exc:  # one bad delivery must not hide the rest
                    record["notified"] = False
                    record["error"] = str(exc)
                    flagged.append(record)
                    continue
                # Only a delivered notification is marked handled. A failed
                # one is retried on the next run, which is the behaviour a
                # user wants from a job that runs while they sleep.
                memory.record_event(item.item_id, event, json.dumps(record, sort_keys=True))
                record["notified"] = True
            else:
                record["notified"] = False

            flagged.append(record)
    finally:
        if owns_memory:
            memory.close()

    if notify:
        _write_state(config, today=today, checked=len(items), flagged=flagged)

    return flagged


def state_status(state_path: str | Path, *, max_age_hours: float = 36.0) -> tuple[int, str]:
    """Is the schedule actually firing?

    Returns `(exit_code, message)`. Exit 0 means the run record exists and
    its `last_run` is inside `max_age_hours`; 1 means missing, unreadable,
    or stale. The default window is 36 hours so a daily job has room for a
    late fire without a false alarm.
    """
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
    """Entrypoint for the Nebius Serverless Job container.

    Reads configuration from the environment, runs one pass, and exits 0.
    The job's schedule is configured on the Nebius side, not here.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report, do not notify")
    parser.add_argument("--look-ahead", type=int, default=None, help="days to look ahead")
    parser.add_argument("--state", default=None, help="run-record path")
    parser.add_argument("--db", default=None, help="memory database path")
    parser.add_argument("--household", default=None, help="household id")
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
    args = parser.parse_args(argv)

    config = JobConfig()
    if args.state:
        config.state_path = args.state
    if args.db:
        config.memory_path = args.db
    if args.household:
        config.household_id = args.household
    if args.look_ahead is not None:
        config.look_ahead_days = args.look_ahead

    if args.status:
        code, message = state_status(config.state_path, max_age_hours=args.max_age_hours)
        print(f"ledger: {message}")
        return code

    if args.dry_run:
        config.notify = False

    today = datetime.now(UTC).date()
    try:
        flagged = run_once(config, today=today)
    except OSError as exc:
        print(f"ledger: could not record the run: {exc}", file=sys.stderr)
        return 1

    print(f"ledger: flagged {len(flagged)} item(s) for {config.household_id}")
    if not config.notify:
        print("ledger: dry run — nothing notified, nothing recorded")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
