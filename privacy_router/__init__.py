"""The privacy router.

Phase 2 — Person A.

Three modules, three jobs:

    redact.py           remove personal details from a payload
    injection_guard.py  decide whether a request should proceed at all
    audit_log.py        normalise OpenShell's audit events into our trail

The router is invoked by the OpenShell supervisor middleware in
`sandbox/middleware/ledger-router/`, not directly by the pipeline. That is
deliberate: the middleware sees every request the sandbox is allowed to
make, including the ones Hermes makes on its own and the ones a prompt
injection tries to make. A library that only our code calls cannot
provide that guarantee.
"""

from privacy_router.audit_log import AuditEvent, AuditLog
from privacy_router.injection_guard import Decision, GuardVerdict
from privacy_router.redact import (
    Category,
    Redaction,
    RedactionMap,
    RedactionResult,
    contains_personal_detail,
    find_spans,
    local_redact,
    redact,
    redacted_text,
)

__all__ = [
    "AuditEvent",
    "AuditLog",
    "Category",
    "Decision",
    "GuardVerdict",
    "Redaction",
    "RedactionMap",
    "RedactionResult",
    "contains_personal_detail",
    "find_spans",
    "local_redact",
    "redact",
    "redacted_text",
]
