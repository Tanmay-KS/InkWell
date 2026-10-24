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

Phase 3 implementation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

__all__ = ["JobConfig", "main", "run_once"]


@dataclass(slots=True)
class JobConfig:
    """Configuration for the deadline monitor.

    Attributes:
        look_ahead_days: How far ahead to flag. A renewal a month out is
            not actionable yet; one inside a week is.
        notify: Whether to send notifications. False for a dry run.
        state_path: Where the job keeps its own run state, on the data
            volume.
        household_id: Which household to check. One household per
            deployment in the demo; the field exists so the multi-household
            version does not need a schema change.
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
    extra: dict[str, object] = field(default_factory=dict)


def run_once(
    config: JobConfig | None = None, *, today: date | None = None
) -> list[dict[str, object]]:
    """Run one pass of the deadline check.

    Returns:
        The items that were flagged, as plain dicts. Empty when there is
        nothing due.

    Phase 3 implementation.
    """
    raise NotImplementedError("Phase 3 — see IMPLEMENTATION.md")


def main() -> int:
    """Entrypoint for the Nebius Serverless Job container.

    Reads configuration from the environment, runs one pass, and exits 0.
    The job's schedule is configured on the Nebius side, not here.
    """
    config = JobConfig()
    today = datetime.now(UTC).date()
    flagged = run_once(config, today=today)
    print(f"ledger: flagged {len(flagged)} item(s) for {config.household_id}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
