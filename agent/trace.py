"""Glass-box trace: one JSON line per agent step.

Upgrade Phase A — the main fix for the "it is a chatbot wrapper" feeling.

Every role in the orchestrator writes a `TraceEvent` when it starts and
when it hands off. The event is the same shape whether the step was a
model call or plain code:

    {
      "run_id": "...", "step": 3, "agent": "Auditor",
      "model": "<nemotron variant>", "tier": "ultra",
      "latency_ms": 0, "tokens_in": 0, "tokens_out": 0,
      "input_summary": "<redacted summary, no raw PII>",
      "output_summary": "<short>",
      "egress": "none | allowed:<host> | blocked:<host>",
      "status": "ok | rejected | needs_human_review | error"
    }

Two rules make the panel trustworthy:

1. **Summaries are redacted on the way in, not on the way out.** A trace
   that stores raw text and masks it in the renderer is one bug away from
   showing a policy number. `safe_summary()` runs the local redactor
   before the event is constructed, and the event keeps only the masked
   string.
2. **The trace is append-only.** A step that is written is not edited
   later; the audit trail and the JSONL file are the same record.

The same events are kept in memory for the web timeline and can be
written to a JSONL file for a recording. No third-party tracing service is
involved; this module is the whole tracer.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from privacy_router.redact import local_redact

__all__ = [
    "STATUSES",
    "TraceEvent",
    "TraceLog",
    "egress_label",
    "safe_summary",
    "timer",
]

STATUSES = ("ok", "rejected", "needs_human_review", "error")

# A summary is a glance, not a transcript. Long enough to recognise the
# step, short enough that a timeline row stays readable.
SUMMARY_LIMIT = 180


def safe_summary(value: object, *, limit: int = SUMMARY_LIMIT) -> str:
    """A short, redacted description of a step's input or output.

    The local redactor runs before truncation so that a personal detail
    is never persisted even in a partial line. Newlines are flattened:
    one event is one JSON line.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if not text:
        return ""
    redacted = local_redact(text).text
    if len(redacted) <= limit:
        return redacted
    return redacted[: limit - 1].rstrip() + "…"


def egress_label(host: str | None = None, *, blocked: bool = False) -> str:
    """The trace's egress field: none, allowed:<host>, or blocked:<host>.

    `host` is the only thing recorded. A full URL can carry a query
    string, and a query string can carry personal detail.
    """
    if not host:
        return "none"
    clean = host.strip().lower()
    return f"blocked:{clean}" if blocked else f"allowed:{clean}"


@dataclass(slots=True)
class TraceEvent:
    """One step in one run."""

    run_id: str
    step: int
    agent: str
    model: str | None = None
    tier: str = "code"
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    input_summary: str = ""
    output_summary: str = ""
    egress: str = "none"
    status: str = "ok"

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unsupported trace status: {self.status}")
        # Summaries are always redacted, whether the caller remembered to
        # use `safe_summary` or not.
        self.input_summary = safe_summary(self.input_summary)
        self.output_summary = safe_summary(self.output_summary)

    def to_dict(self) -> dict[str, object]:
        """The event in the documented schema, key order included."""
        return {
            "run_id": self.run_id,
            "step": self.step,
            "agent": self.agent,
            "model": self.model,
            "tier": self.tier,
            "latency_ms": self.latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "egress": self.egress,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TraceEvent:
        return cls(
            run_id=str(data.get("run_id", "")),
            step=int(data.get("step", 0) or 0),
            agent=str(data.get("agent", "")),
            model=(str(data["model"]) if data.get("model") else None),
            tier=str(data.get("tier", "code")),
            latency_ms=int(data.get("latency_ms", 0) or 0),
            tokens_in=int(data.get("tokens_in", 0) or 0),
            tokens_out=int(data.get("tokens_out", 0) or 0),
            input_summary=str(data.get("input_summary", "")),
            output_summary=str(data.get("output_summary", "")),
            egress=str(data.get("egress", "none")),
            status=str(data.get("status", "ok")),
        )


@dataclass(slots=True)
class TraceLog:
    """The per-run event list, optionally mirrored to a JSONL file."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    path: Path | None = None
    events: list[TraceEvent] = field(default_factory=list)

    def record(
        self,
        agent: str,
        *,
        model: str | None = None,
        tier: str = "code",
        latency_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        input_summary: object = "",
        output_summary: object = "",
        egress: str = "none",
        status: str = "ok",
    ) -> TraceEvent:
        """Append one step and return it."""
        event = TraceEvent(
            run_id=self.run_id,
            step=len(self.events) + 1,
            agent=agent,
            model=model,
            tier=tier,
            latency_ms=max(0, int(latency_ms)),
            tokens_in=max(0, int(tokens_in)),
            tokens_out=max(0, int(tokens_out)),
            input_summary=str(input_summary),
            output_summary=str(output_summary),
            egress=egress,
            status=status,
        )
        self.events.append(event)
        self._append(event)
        return event

    def timed(
        self,
        agent: str,
        *,
        started: float,
        **kwargs: object,
    ) -> TraceEvent:
        """Record a step whose wall-clock time the caller measured."""
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return self.record(agent, latency_ms=elapsed_ms, **kwargs)  # type: ignore[arg-type]

    def _append(self, event: TraceEvent) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def to_dicts(self) -> list[dict[str, object]]:
        return [event.to_dict() for event in self.events]

    def summary(self) -> dict[str, object]:
        """Aggregates the UI and the README can quote."""
        by_agent: dict[str, int] = {}
        egress: dict[str, int] = {}
        for event in self.events:
            by_agent[event.agent] = by_agent.get(event.agent, 0) + 1
            egress[event.egress] = egress.get(event.egress, 0) + 1
        return {
            "run_id": self.run_id,
            "steps": len(self.events),
            "agents": by_agent,
            "egress": egress,
            "latency_ms": sum(event.latency_ms for event in self.events),
            "model_calls": sum(1 for event in self.events if event.model),
        }

    def read(self, path: Path | None = None) -> list[TraceEvent]:
        """Load events from a JSONL file written by this class."""
        target = path or self.path
        if target is None:
            return list(self.events)
        events: list[TraceEvent] = []
        with target.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(TraceEvent.from_dict(json.loads(line)))
        return events


class timer:
    """Small wall-clock helper: `with timer() as t: ...; t.ms`.

    Kept here so every role measures latency the same way. It is not a
    context manager that swallows exceptions — a failing step still has a
    latency, and the caller records the error event.
    """

    __slots__ = ("_started", "ms")

    def __init__(self) -> None:
        self._started = 0.0
        self.ms = 0

    def __enter__(self) -> timer:
        self._started = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = int((time.perf_counter() - self._started) * 1000)


def redact_trace_values(events: Iterable[TraceEvent]) -> list[TraceEvent]:
    """Return copies with every summary re-redacted.

    A defensive pass for events loaded from disk or built by hand. It is
    idempotent: a placeholder stays a placeholder.
    """
    return [
        TraceEvent(
            run_id=event.run_id,
            step=event.step,
            agent=event.agent,
            model=event.model,
            tier=event.tier,
            latency_ms=event.latency_ms,
            tokens_in=event.tokens_in,
            tokens_out=event.tokens_out,
            input_summary=event.input_summary,
            output_summary=event.output_summary,
            egress=event.egress,
            status=event.status,
        )
        for event in events
    ]
