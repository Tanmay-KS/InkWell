"""Prompt-injection and exfiltration guard.

Phase 2 — Person A.

The threat: an untrusted document, email, or web page tells the agent to
send data somewhere. The agent, being a language model that follows
instructions, complies. The sandbox stops it.

Two layers, and the second is the one that matters:

1. **The network policy** already prevents the agent reaching any host
   that is not on the allowlist. That is the primary control and it is
   enforced by the runtime, not by us.

2. **This module** is the content-aware layer that works *inside* the
   allowlist. A request to `api.tavily.com` is permitted by policy — but
   if its body contains an account number that the agent extracted from a
   document two steps earlier, that request should never have been made.
   This is what the middleware calls.

The decision this module makes is a three-way one, not a boolean:

    ALLOW    the request carries no personal detail beyond what the
             redaction layer expects to have handled
    REDACT   the request carries personal detail that can be masked —
             the normal case, and the router's job
    DENY     the request looks like an exfiltration attempt: personal
             detail going to a destination it has no business being at,
             or a destination that was never associated with the task

The DENY case is what the video shows. It must be rare, it must be
explainable, and it must be logged with a reason code.

Phase 2 implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Decision", "GuardVerdict", "evaluate_request"]


class Decision(StrEnum):
    """What to do with a request."""

    ALLOW = "allow"
    REDACT = "redact"
    DENY = "deny"


@dataclass(slots=True)
class GuardVerdict:
    """The outcome of evaluating one request.

    Attributes:
        decision: What the middleware should do.
        reason_code: A short, stable, machine-readable reason. This ends
            up in the audit log and in the demo, so it must be a code and
            not a sentence.
        detail: A human-readable explanation. Never contains the personal
            detail itself.
        findings: What was detected, without the values.
    """

    decision: Decision
    reason_code: str
    detail: str = ""
    findings: list[str] = field(default_factory=list)


# Reason codes. Stable strings — they end up in the audit log and in the
# video's on-screen output.
REASON_CLEAN = "no_personal_detail"
REASON_REDACTED = "personal_detail_redacted"
REASON_EXFIL_ATTEMPT = "personal_detail_to_unexpected_destination"
REASON_UNKNOWN_DESTINATION = "destination_not_associated_with_task"


def evaluate_request(
    *,
    host: str,
    path: str,
    body: str,
    model: str | None = None,
    task_hosts: set[str] | None = None,
) -> GuardVerdict:
    """Decide whether an outbound request should proceed.

    Args:
        host: The destination host.
        path: The request path.
        body: The request body, as text.
        model: The model to use for the semantic pass.
        task_hosts: The hosts this task legitimately talks to. A request to
            a host outside this set that carries personal detail is an
            exfiltration attempt rather than a search.

    Returns:
        A `GuardVerdict`.

    Phase 2 implementation.
    """
    raise NotImplementedError("Phase 2 — see IMPLEMENTATION.md")
