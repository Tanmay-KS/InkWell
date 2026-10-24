"""Notification adapters for the deadline monitor.

The monitor owns *what* to notify about; a notifier owns *how*. Keeping
the two apart matters because the live job runs on a machine with no chat
client, while the test suite must run with no network at all.

Two adapters ship here:

* `stdout_notifier()` writes a structured, deliberately safe event to the
  Serverless Job log. It includes the item id, provider, kind, renewal
  date and days remaining, but never the holder or the account reference.
  A job log is operational telemetry, not the user's private channel.
* `command_notifier()` pipes the full notice as JSON to a configured
  executable. That is the seam a Telegram/Slack adapter plugs into. It
  uses `subprocess.run` with an argv list, never a shell, and it does not
  put the command's stdout/stderr into the exception.

The full notice is the user-facing payload. It is allowed to contain the
holder and reference because it is on its way to the user's own channel.
The safe event and the job state are not.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Mapping
from typing import Any, Protocol, TextIO

__all__ = [
    "SAFE_FIELDS",
    "Notifier",
    "command_notifier",
    "stdout_notifier",
]


class Notifier(Protocol):
    """A callable that delivers one deadline notice."""

    def __call__(self, notice: Mapping[str, Any]) -> None: ...


# Fields that are safe to put in an operational log. Holder names and
# account references are deliberately absent.
SAFE_FIELDS = (
    "household_id",
    "item_id",
    "kind",
    "provider",
    "renewal_date",
    "days_until",
)


def stdout_notifier(stream: TextIO | None = None) -> Notifier:
    """Return a notifier that logs only the non-personal fields.

    The output is one JSON object per line, which is what a Serverless
    Job's log viewer can filter without a parser.
    """
    output = stream or sys.stdout

    def send(notice: Mapping[str, Any]) -> None:
        safe = {key: notice.get(key) for key in SAFE_FIELDS if key in notice}
        print(
            json.dumps({"event": "deadline_notification", **safe}, sort_keys=True),
            file=output,
            flush=True,
        )

    return send


def command_notifier(command: str, *, timeout: float = 30.0) -> Notifier:
    """Return a notifier that pipes the notice to an external command.

    `command` is parsed with `shlex.split`, so the caller can provide a
    program plus arguments without invoking a shell. The full notice is
    written to stdin as a single JSON object.

    A non-zero exit raises `RuntimeError`; a command that runs longer than
    `timeout` raises `subprocess.TimeoutExpired`. The command's output is
    discarded rather than included in the exception: it may contain the
    personal detail the notice is about, and a serverless job should not
    hold an unbounded amount of it in memory.
    """
    argv = shlex.split(command)
    if not argv:
        raise ValueError("notification command is empty")
    if timeout <= 0:
        raise ValueError("notification timeout must be positive")

    def send(notice: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(notice), sort_keys=True, default=str)
        process = subprocess.run(
            argv,
            input=payload,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"notification command exited with status {process.returncode}"
            )

    return send
