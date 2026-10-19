"""Audit trail.

Phase 2 — Person A.

The audit log is **consumed from OpenShell**, not authored by us. OpenShell
records every middleware invocation in its OCSF event stream: the policy
entry, the middleware name, the decision, whether the traffic was changed,
and any failure. It also records network policy denials, credential
substitution events, and configuration changes.

That is a stronger position than writing our own log: the entries are
produced by the runtime that enforced the decision, so they cannot be
faked by the thing being audited.

This module's job is to normalise that stream into the project's audit
trail — one JSON object per line, a stable shape, and a redaction pass of
its own so that the audit log does not become a second place where personal
details are stored in the clear.

The audit log is deliberately NOT where we prove the leak rate. The leak
test (`eval/leak_test.py`) measures outbound payloads directly. The audit
log is for showing a human what the router did and why.

Phase 2 implementation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["AuditEvent", "AuditLog", "from_ocsf"]


@dataclass(slots=True)
class AuditEvent:
    """One normalised audit entry.

    Attributes:
        timestamp: When the event happened, in UTC.
        event_type: What happened — "router.redacted", "router.denied",
            "network.denied", "credential.substituted", …
        decision: The outcome, when there was one.
        reason_code: Why, when there was a reason.
        host: The destination host.
        detail: A human-readable note, with personal details removed.
        raw: The original OCSF record, kept for forensics.
    """

    timestamp: str
    event_type: str
    decision: str | None = None
    reason_code: str | None = None
    host: str | None = None
    detail: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)


class AuditLog:
    """An append-only JSONL audit trail.

    Attributes:
        path: Where the log is written.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: AuditEvent) -> None:
        """Append one event.

        Append-only, and opened in append mode on every write: an audit
        log that can be rewritten is not an audit log.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.to_json() + "\n")

    def read_all(self) -> list[AuditEvent]:
        """Read every event back."""
        if not self.path.exists():
            return []
        events: list[AuditEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            events.append(
                AuditEvent(
                    timestamp=data.get("timestamp", ""),
                    event_type=data.get("event_type", ""),
                    decision=data.get("decision"),
                    reason_code=data.get("reason_code"),
                    host=data.get("host"),
                    detail=data.get("detail"),
                    raw=data.get("raw", {}),
                )
            )
        return events

    def summary(self) -> dict[str, int]:
        """Count events by type and decision.

        This is the shape the video's on-screen panel shows.
        """
        counts: dict[str, int] = {}
        for event in self.read_all():
            key = event.event_type
            if event.decision:
                key = f"{key}:{event.decision}"
            counts[key] = counts.get(key, 0) + 1
        return counts


def from_ocsf(records: Iterable[dict[str, Any]]) -> list[AuditEvent]:
    """Normalise OpenShell OCSF records into audit events.

    Phase 2 implementation — the mapping depends on the exact OCSF event
    shapes OpenShell emits for middleware invocations and network denials.
    """
    raise NotImplementedError("Phase 2 — see IMPLEMENTATION.md")


def now_iso() -> str:
    """The current time in UTC, ISO 8601."""
    return datetime.now(UTC).isoformat()
