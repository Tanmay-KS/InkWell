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

The guard is deliberately a pure function of the request. It does not
know which document the body came from, and it does not need to: the
rules below are about the shape of the request, not about intent. That is
what makes the decision testable and what makes it impossible for the
agent to talk its way around.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from privacy_router.redact import contains_personal_detail

__all__ = [
    "Decision",
    "GuardPolicy",
    "GuardVerdict",
    "evaluate_request",
]


class Decision(StrEnum):
    """What to do with a request.

    The value goes straight into the audit log and the gRPC result without
    a conversion step.
    """

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

    decision: str
    reason_code: str
    detail: str = ""
    findings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GuardPolicy:
    """The policy the guard evaluates against.

    Every field is deliberately data, not code: a policy that can only be
    changed by editing Python is a policy that will drift away from the
    sandbox's `policy.yaml`. The middleware builds one of these from the
    policy config, so the two stay in step.

    Attributes:
        allowed_hosts: Hosts the network policy permits. A request to a
            host outside this set has escaped the policy layer and is
            denied rather than evaluated.
        task_hosts: Hosts this task legitimately talks to. When empty,
            every allowed host is treated as a task host.
        deny_hosts: Hosts that must never receive personal detail. This is
            how a deployment says "Tavily is for this task, Telegram is
            not" without writing a new rule.
        third_party_hosts: Hosts that are third parties, where a bulk
            dump of personal detail is not a search query.
        ocr_models: Models allowed to receive a document image. An image
            cannot be redacted, so it only goes to a model that is
            expected to read it.
        forbidden_flags: Request-body flags that turn a third-party call
            into a model echo. Tavily's `include_answer` and
            `include_raw_content` are the two that matter.
        deny_threshold: How many distinct categories of personal detail
            make a third-party request a bulk dump rather than a query.
    """

    allowed_hosts: tuple[str, ...] = ()
    task_hosts: tuple[str, ...] = ()
    deny_hosts: tuple[str, ...] = ()
    third_party_hosts: tuple[str, ...] = ("api.tavily.com",)
    ocr_models: tuple[str, ...] = ()
    forbidden_flags: tuple[str, ...] = ("include_answer", "include_raw_content")
    deny_threshold: int = 3


# Reason codes. Stable strings — they end up in the audit log and in the
# video's on-screen output.
REASON_CLEAN = "no_personal_detail"
REASON_REDACTED = "personal_detail_redacted"
REASON_EXFIL_ATTEMPT = "personal_detail_to_unexpected_destination"
REASON_UNKNOWN_DESTINATION = "destination_not_associated_with_task"
REASON_THIRD_PARTY_ECHO = "third_party_model_echo_forbidden"
REASON_BULK_EXFIL = "bulk_personal_detail_to_third_party"
REASON_IMAGE_WRONG_MODEL = "image_to_unapproved_model"
REASON_DENY_HOST = "destination_forbidden_for_personal_detail"


def _lower_set(values: Iterable[str]) -> set[str]:
    return {value.strip().lower() for value in values if value and value.strip()}


def evaluate_request(
    *,
    host: str,
    path: str,
    body: str,
    model: str | None = None,
    task_hosts: set[str] | None = None,
    policy: GuardPolicy | None = None,
    has_image: bool = False,
    flags: Iterable[str] = (),
    names: Iterable[str] = (),
) -> GuardVerdict:
    """Decide whether an outbound request should proceed.

    Args:
        host: The destination host.
        path: The request path.
        body: The request body, as text.
        model: The model the request is aimed at, when it is a model call.
        task_hosts: The hosts this task legitimately talks to. A request to
            a host outside this set that carries personal detail is an
            exfiltration attempt rather than a search. Kept for callers
            that predate `GuardPolicy`; `policy.task_hosts` wins when both
            are supplied.
        policy: The full policy. When omitted, a permissive policy is used
            so a direct caller still gets useful REDACT/ALLOW decisions
            without having to construct one.
        has_image: True when the body carries a document image. An image
            cannot be redacted, so it is only allowed to an OCR model.
        flags: Request-body flags that are present (for example Tavily's
            `include_answer`). The body parser supplies these because the
            guard deliberately does not parse JSON.
        names: Names known to be personal — the household's names.

    Returns:
        A `GuardVerdict`.
    """
    active = policy or GuardPolicy()
    destination = (host or "").strip().lower()
    categories = sorted(
        category.value
        for category in contains_personal_detail(body, names=names)
    )

    allowed = _lower_set(active.allowed_hosts)
    if allowed and destination not in allowed:
        return GuardVerdict(
            decision=Decision.DENY,
            reason_code=REASON_EXFIL_ATTEMPT,
            detail=f"{destination or 'unknown host'} is not on the egress allowlist",
            findings=categories,
        )

    if destination in _lower_set(active.deny_hosts):
        return GuardVerdict(
            decision=Decision.DENY,
            reason_code=REASON_DENY_HOST,
            detail=f"{destination} must not receive personal detail",
            findings=categories,
        )

    if has_image:
        ocr_models = _lower_set(active.ocr_models)
        if not ocr_models or (model or "").strip().lower() not in ocr_models:
            return GuardVerdict(
                decision=Decision.DENY,
                reason_code=REASON_IMAGE_WRONG_MODEL,
                detail="a document image may only go to the approved OCR model",
                findings=categories,
            )

    if not categories:
        return GuardVerdict(
            decision=Decision.ALLOW,
            reason_code=REASON_CLEAN,
            detail="no personal detail in the request",
        )

    expected = task_hosts if task_hosts is not None else active.task_hosts
    if expected and destination not in _lower_set(expected):
        return GuardVerdict(
            decision=Decision.DENY,
            reason_code=REASON_UNKNOWN_DESTINATION,
            detail=f"{destination} is not associated with this task",
            findings=categories,
        )

    forbidden = _lower_set(active.forbidden_flags)
    present = {str(flag).strip().lower() for flag in flags}
    if destination in _lower_set(active.third_party_hosts) and present & forbidden:
        matched = ", ".join(sorted(present & forbidden))
        return GuardVerdict(
            decision=Decision.DENY,
            reason_code=REASON_THIRD_PARTY_ECHO,
            detail=f"third-party model echo requested ({matched})",
            findings=categories,
        )

    if (
        destination in _lower_set(active.third_party_hosts)
        and len(categories) >= max(1, active.deny_threshold)
    ):
        return GuardVerdict(
            decision=Decision.DENY,
            reason_code=REASON_BULK_EXFIL,
            detail=(
                f"{len(categories)} categories of personal detail in one "
                f"third-party request (threshold {active.deny_threshold})"
            ),
            findings=categories,
        )

    safe_path = (path or "").split("?", 1)[0].split("#", 1)[0]
    return GuardVerdict(
        decision=Decision.REDACT,
        reason_code=REASON_REDACTED,
        detail=f"personal detail found in {safe_path or 'the request'} and will be masked",
        findings=categories,
    )
