"""Audit trail.

Phase 2 — Person A.

The audit trail is **normalised from the enforcement path**. OpenShell
records every middleware invocation in its OCSF event stream: the policy
entry, the middleware name, the decision, whether the traffic was changed,
and any failure. It also records network policy denials, credential
substitution events, and configuration changes. The middleware itself
appends a per-request JSONL at the same seam, with the stable reason code
and category counts.

Both are produced by the process that enforced the decision, not by the
agent being audited. This module does not invent entries; it maps the
runtime's records into one stable shape.

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
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
        raw: A whitelist of the OCSF decision fields, not the whole
            record. Free text and request data are deliberately excluded.
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

    def ensure_writable(self) -> bool:
        """Report whether an event can be appended.

        The middleware calls this before it allows anything: an allow that
        cannot be recorded is not an allow. Opening in append mode is the
        real check; ``os.access`` lies under ACLs and on read-only mounts.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8"):
                pass
            return True
        except OSError:
            return False

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

    The input is what ``openshell settings set ledger-hermes --key
    ocsf_json_enabled --value true`` produces: one JSON object per line,
    with the OCSF classes OpenShell emits for HTTP Activity (4002), Network
    Activity (4001) and Detection Finding (2004).

    Two rules shape the mapping:

    * **The OCSF event is the source of truth for what the runtime did.**
      A middleware invocation carries ``action_id``, ``disposition_id``,
      ``unmapped.transformed`` and ``unmapped.failed``; a denial carries a
      stable ``status_detail`` of the form
      ``middleware_denied:<config>:<reason_code>``. The importer reads
      those, not the middleware's own claims.
    * **The audit log must not become a second copy of the data.** The
      original record is not stored wholesale: ``raw`` is a whitelist of
      decision fields, and every human-readable string goes through the
      deterministic redactor before it is written.

    Unknown and malformed records are skipped rather than raising. One bad
    line in a log export must not stop the rest of the trail from being
    read.
    """
    from privacy_router.redact import local_redact

    events: list[AuditEvent] = []
    for record in records:
        if isinstance(record, (str, bytes)):
            try:
                record = json.loads(record)
            except (TypeError, ValueError):
                continue
        if not isinstance(record, dict):
            continue

        class_uid = _as_int(record.get("class_uid"))
        if class_uid is None:
            continue

        timestamp = _ocsf_time(record)
        host = _ocsf_host(record)
        unmapped = _as_dict(record.get("unmapped"))
        action = _as_int(record.get("action_id"))
        disposition = _as_int(record.get("disposition_id"))
        transformed = _truthy(unmapped.get("transformed"))
        failed = _truthy(unmapped.get("failed"))
        middleware = _middleware_name(record, unmapped)
        status_detail = _as_text(record.get("status_detail"))
        reason_code = _reason_code(status_detail)
        message = _as_text(record.get("message"))

        if class_uid == 2004:
            finding = _as_dict(record.get("finding_info"))
            uid = _as_text(finding.get("uid")) or "ocsf.finding"
            title = _scrub(local_redact, _as_text(finding.get("title")) or "")
            event_type = (
                "router.finding"
                if uid.startswith("openshell.middleware") or middleware
                else "network.finding"
            )
            decision = "deny" if (action == 2 or disposition == 2) else None
            events.append(
                AuditEvent(
                    timestamp=timestamp,
                    event_type=event_type,
                    decision=decision,
                    reason_code=reason_code or uid,
                    host=host,
                    detail=title or None,
                    raw=_safe_raw(record, class_uid, unmapped),
                )
            )
            continue

        if class_uid == 4002:
            if middleware or transformed or failed:
                if action == 2 or disposition == 2:
                    event_type = "router.failed_closed" if failed else "router.denied"
                    decision = "deny"
                elif failed:
                    event_type = "router.failed_open"
                    decision = "allow"
                elif transformed:
                    event_type = "router.redacted"
                    decision = "redact"
                else:
                    event_type = "router.allowed"
                    decision = "allow"
            else:
                event_type = "network.denied" if (action == 2 or disposition == 2) else "network.allowed"
                decision = "deny" if event_type == "network.denied" else "allow"

            if not reason_code:
                reason_code = _default_reason(event_type, transformed)
            detail = _scrub(local_redact, status_detail or message or "")
            events.append(
                AuditEvent(
                    timestamp=timestamp,
                    event_type=event_type,
                    decision=decision,
                    reason_code=reason_code,
                    host=host,
                    detail=detail or None,
                    raw=_safe_raw(record, class_uid, unmapped),
                )
            )
            continue

        if class_uid == 4001:
            denied = action == 2 or disposition == 2
            event_type = "network.denied" if denied else "network.allowed"
            events.append(
                AuditEvent(
                    timestamp=timestamp,
                    event_type=event_type,
                    decision="deny" if denied else "allow",
                    reason_code=reason_code or ("request_denied" if denied else "allowed"),
                    host=host,
                    detail=_scrub(local_redact, status_detail or message or "") or None,
                    raw=_safe_raw(record, class_uid, unmapped),
                )
            )
            continue

        events.append(
            AuditEvent(
                timestamp=timestamp,
                event_type="ocsf.event",
                decision=None,
                reason_code=None,
                host=host,
                detail=f"OCSF class {class_uid}",
                raw=_safe_raw(record, class_uid, unmapped),
            )
        )
    return events


# --- OCSF helpers ---------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _ocsf_time(record: dict[str, Any]) -> str:
    """OCSF `time` is milliseconds since the epoch."""
    value = record.get("time")
    milliseconds = _as_int(value)
    if milliseconds is None:
        text = _as_text(value)
        if text:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).isoformat()
            except ValueError:
                return ""
        return ""
    try:
        seconds = milliseconds / 1000.0
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _ocsf_host(record: dict[str, Any]) -> str | None:
    for endpoint_name in ("dst_endpoint", "proxy_endpoint"):
        endpoint = _as_dict(record.get(endpoint_name))
        for key in ("domain", "hostname", "ip", "name"):
            host = _as_text(endpoint.get(key))
            if host and host.lower() != "unknown":
                return host
    request = _as_dict(record.get("http_request"))
    url = request.get("url")
    if isinstance(url, dict):
        host = _as_text(url.get("hostname"))
        if host:
            return host
    elif isinstance(url, str):
        parsed = urlsplit(url)
        if parsed.hostname:
            return parsed.hostname
    unmapped = _as_dict(record.get("unmapped"))
    return _as_text(unmapped.get("host"))


def _middleware_name(record: dict[str, Any], unmapped: dict[str, Any]) -> str | None:
    for key in ("middleware", "middleware_config"):
        name = _as_text(unmapped.get(key))
        if name:
            return name
    firewall = _as_dict(record.get("firewall_rule"))
    if _as_text(firewall.get("type")) == "middleware":
        return _as_text(firewall.get("name"))
    message = _as_text(record.get("message")) or ""
    if message.startswith("MIDDLEWARE "):
        parts = message.split()
        if len(parts) >= 2:
            return parts[1]
    return None


def _reason_code(status_detail: str | None) -> str | None:
    if not status_detail:
        return None
    if status_detail.startswith("middleware_denied:"):
        parts = status_detail.split(":")
        if len(parts) >= 3 and parts[2]:
            return parts[2]
    if status_detail.startswith("middleware_failed:"):
        return "middleware_failed"
    if _STABLE_CODE.fullmatch(status_detail):
        return status_detail
    return None


def _default_reason(event_type: str, transformed: bool) -> str:
    if event_type == "router.redacted":
        return "personal_detail_redacted"
    if event_type == "router.finding":
        return "middleware_finding"
    if event_type in {"router.denied", "router.failed_closed"}:
        return "request_denied"
    if event_type == "router.failed_open":
        return "middleware_failed"
    if event_type == "router.allowed":
        return "no_personal_detail"
    if event_type == "network.denied":
        return "network_policy_denied"
    if transformed:
        return "body_transformed"
    return "allowed"


def _scrub(redact_fn, text: str) -> str:
    """Make a human-readable field safe to store.

    The OCSF importer is a boundary: whatever was in the gateway's log is
    treated as untrusted, because a future middleware finding might carry a
    label derived from request content. The deterministic pass is enough
    here; it never calls a model and it never throws.
    """
    if not text:
        return ""
    try:
        return redact_fn(text).text
    except Exception:  # pragma: no cover - defensive boundary
        return "[unavailable]"


def _safe_raw(
    record: dict[str, Any], class_uid: int, unmapped: dict[str, Any]
) -> dict[str, Any]:
    """A whitelist of decision fields, never the whole record.

    Deliberately excluded: `http_request` (the URL can carry a query),
    `message` and `status_detail` (free text), and any request body.
    """
    finding = _as_dict(record.get("finding_info"))
    firewall = _as_dict(record.get("firewall_rule"))
    safe: dict[str, Any] = {
        "class_uid": class_uid,
        "activity_id": _as_int(record.get("activity_id")),
        "action_id": _as_int(record.get("action_id")),
        "disposition_id": _as_int(record.get("disposition_id")),
        "severity_id": _as_int(record.get("severity_id")),
        "status_id": _as_int(record.get("status_id")),
        "unmapped": {
            key: unmapped[key]
            for key in ("transformed", "failed", "middleware", "middleware_config", "count")
            if key in unmapped
        },
    }
    if finding:
        # `title` is free text; `detail` already carries a scrubbed copy.
        safe["finding_info"] = {
            key: finding[key] for key in ("uid",) if key in finding
        }
    if firewall:
        safe["firewall_rule"] = {
            key: firewall[key] for key in ("name", "type") if key in firewall
        }
    return {key: value for key, value in safe.items() if value not in (None, {}, [])}

_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def now_iso() -> str:
    """The current time in UTC, ISO 8601."""
    return datetime.now(UTC).isoformat()
