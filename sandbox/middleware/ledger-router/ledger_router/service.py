"""The gRPC service OpenShell calls for every allowed HTTP request.

This is a thin transport. It parses the policy config, hands the body to
`ledger_router.body` and the decision to `privacy_router.injection_guard`,
writes one audit line, and returns the result. Nothing here formats a
message, calls a model, or knows what a document is.

The one design rule that matters: **a body is never logged, echoed in a
reason, or put in an exception message**. The audit entry carries a hash
and category counts. The reason codes are the stable vocabulary the video
and the audit trail share.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import grpc
from generated import extension_pb2
from generated import supervisor_middleware_pb2 as pb
from generated import supervisor_middleware_pb2_grpc as pb_grpc
from google.protobuf.json_format import MessageToDict

from ledger_router.body import BodyResult, process_body
from ledger_router.config import RouterConfig
from privacy_router.audit_log import AuditEvent, AuditLog, now_iso
from privacy_router.egress import TAVILY_HOST, TOKEN_FACTORY_HOST, http_transport
from privacy_router.injection_guard import Decision, evaluate_request
from privacy_router.redact import RedactionError

# The audit vocabulary is past tense, matching the OCSF normaliser and the
# docs: "router.allowed", "router.redacted", "router.denied". The guard's
# Decision values are imperatives ("allow"/"redact"/"deny"); do not leak
# that difference into the durable trail.
_AUDIT_EVENT_TYPES = {
    "allow": "router.allowed",
    "redact": "router.redacted",
    "deny": "router.denied",
}

__all__ = ["LedgerRouterServicer", "serve"]

LOG = logging.getLogger("ledger_router")

SERVICE_NAME = "ledger-router"
IMPLEMENTATION_NAME = "inkwell/privacy-router"
IMPLEMENTATION_VERSION = "0.1.0"
CONTRACT_CAPABILITY = "openshell.supervisor-middleware.contract"

# Safety net for a code that would be rejected by OpenShell's format check
# (`^[a-z][a-z0-9_]{0,63}$`). The guard's codes already conform; this is for
# the paths where an exception produces the code.
REASON_INVALID_CONFIG = "invalid_configuration"
REASON_BINARY_BODY = "binary_body_not_inspectable"
REASON_REDACTION_NOT_APPLIED = "redaction_not_applied"
REASON_ESCALATION_FAILED = "escalation_failed"
REASON_AUDIT_UNAVAILABLE = "audit_unavailable"
REASON_INTERNAL_ERROR = "router_internal_error"


def _safe_path(path: str) -> str:
    """The path without its query or fragment.

    A GET can carry the whole search query in the URL. The audit log
    records the destination shape, never the query.
    """
    value = (path or "").split("?", 1)[0].split("#", 1)[0]
    return value[:200]


def _safe_reason_code(code: str) -> str:
    candidate = (code or REASON_INTERNAL_ERROR).strip().lower()
    if not candidate or not candidate[0].isalpha():
        return REASON_INTERNAL_ERROR
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in candidate)
    return cleaned[:64]


def _struct_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        return MessageToDict(value)
    except Exception:  # pragma: no cover - malformed protobuf is a gateway bug
        return {}


def _validation_reason(exc: Exception) -> str:
    """A config rejection that names fields, never values.

    Pydantic's string form includes the offending input. The input is a
    policy field (a path, a model ID), not a document, but the rule from
    F-039 is the same one that applies everywhere else: report the type
    and the location, not the data.
    """
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            locations = [
                ".".join(str(part) for part in error.get("loc", ()))
                for error in errors()
            ]
            fields = ", ".join(location for location in locations if location)
            if fields:
                return f"invalid ledger-router config: {fields}"
        except Exception:  # pragma: no cover - defensive
            pass
    return f"invalid ledger-router config: {type(exc).__name__}"


class _NameCache:
    """Read the household names from a host-side file, cached by mtime.

    The names are the one piece of personal data the router needs, and
    they are deliberately not in the committed policy or in an environment
    variable that shows up in `ps`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: str | None = None
        self._mtime: float | None = None
        self._names: list[str] = []

    def get(self, path: str | None) -> list[str]:
        if not path:
            return []
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return []
        with self._lock:
            if path == self._path and mtime == self._mtime:
                return self._names
            try:
                names = [
                    line.strip()
                    for line in Path(path).read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except OSError as exc:
                LOG.warning("names file unavailable (%s)", type(exc).__name__)
                return []
            self._path, self._mtime, self._names = path, mtime, names
            return names


class LedgerRouterServicer(pb_grpc.SupervisorMiddlewareServicer):
    """The privacy router as an OpenShell supervisor middleware."""

    def __init__(
        self,
        *,
        default_config: RouterConfig | None = None,
        audit_log: AuditLog | None = None,
        max_payload_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.default_config = default_config or RouterConfig()
        self.max_payload_bytes = max(1, max_payload_bytes)
        self._audit_lock = threading.Lock()
        self._names = _NameCache()
        self._audit_log: AuditLog | None = audit_log
        self._audit_path = ""

    # -- discovery ---------------------------------------------------------

    def Describe(self, request, context):
        return pb.MiddlewareManifest(
            name=SERVICE_NAME,
            service_version=IMPLEMENTATION_VERSION,
            bindings=[
                pb.MiddlewareBinding(
                    operation=pb.SUPERVISOR_MIDDLEWARE_OPERATION_HTTP_REQUEST,
                    phase=pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
                    max_payload_bytes=self.max_payload_bytes,
                )
            ],
            expected_audience="",
            extension=extension_pb2.PeerMetadata(
                protocol_version=extension_pb2.ProtocolVersion(major=1, minor=0),
                implementation_name=IMPLEMENTATION_NAME,
                implementation_version=IMPLEMENTATION_VERSION,
                supported_capabilities=[CONTRACT_CAPABILITY],
            ),
        )

    def ValidateConfig(self, request, context):
        try:
            config = RouterConfig(**_struct_to_dict(request.config))
        except Exception as exc:
            # The reason is shown to the operator applying the policy. It
            # names a field, not a value; pydantic errors do not include
            # request data because there is none at this point.
            return pb.ValidateConfigResponse(
                valid=False,
                reason=_validation_reason(exc)[:2000],
            )
        if config.max_payload_bytes > self.max_payload_bytes:
            return pb.ValidateConfigResponse(
                valid=False,
                reason=(
                    "max_payload_bytes exceeds the registration limit "
                    f"({self.max_payload_bytes})"
                ),
            )
        return pb.ValidateConfigResponse(valid=True)

    # -- evaluation --------------------------------------------------------

    def EvaluateHttpRequest(self, request, context):
        started = time.monotonic()
        config = self._config_for(request)
        if config is None:
            return pb.HttpRequestResult(
                decision=pb.DECISION_DENY,
                reason_code=REASON_INVALID_CONFIG,
                reason="the policy configuration is not valid for this service",
            )

        # The audit log is part of the enforcement, not a side effect. Do
        # this before inspecting the body so an unwritable path denies even
        # a clean request. The gateway's OCSF stream is a second record of
        # the denial; this service cannot write its own.
        if config.audit_log_path and not AuditLog(config.audit_log_path).ensure_writable():
            LOG.error("the audit log is not writable; denying the request")
            return pb.HttpRequestResult(
                decision=pb.DECISION_DENY,
                reason_code=REASON_AUDIT_UNAVAILABLE,
                reason="the privacy router cannot record this request",
            )

        target = request.target
        host = (target.host or "").strip().lower()
        path = target.path or ""
        try:
            result = process_body(
                host=host,
                path=path,
                raw=bytes(request.body),
                config=config,
                names=self._names.get(config.names_file),
                transport=self._escalation_transport(config),
            )
        except Exception as exc:
            LOG.warning("body processing failed (%s)", type(exc).__name__)
            # A RedactionError means the model-assisted check was requested
            # and did not complete. That is a privacy failure, not merely an
            # internal error: deny, and never forward the unredacted body.
            escalation_failed = isinstance(exc, RedactionError) or config.escalate
            reason_code = (
                REASON_ESCALATION_FAILED if escalation_failed else REASON_INTERNAL_ERROR
            )
            self._audit(
                config,
                request=request,
                decision=Decision.DENY,
                reason_code=reason_code,
                detail="body processing failed",
                categories=[],
                duration_ms=_elapsed_ms(started),
                sha256="",
            )
            return pb.HttpRequestResult(
                decision=pb.DECISION_DENY,
                reason_code=_safe_reason_code(reason_code),
                reason="the router could not inspect the request body",
            )

        body_text = _decoded_body(request.body)
        if result.binary and host in {TOKEN_FACTORY_HOST, TAVILY_HOST}:
            self._audit(
                config,
                request=request,
                decision=Decision.DENY,
                reason_code=REASON_BINARY_BODY,
                detail="a binary body cannot be inspected",
                categories=[],
                duration_ms=_elapsed_ms(started),
                sha256=result.sha256,
            )
            return pb.HttpRequestResult(
                decision=pb.DECISION_DENY,
                reason_code=REASON_BINARY_BODY,
                reason="request denied by the InkWell privacy router",
            )

        verdict = evaluate_request(
            host=host,
            path=path,
            body=body_text,
            model=result.model,
            policy=config.as_guard_policy(),
            has_image=result.has_image,
            flags=result.flags,
            names=self._names.get(config.names_file),
        )

        outcome = self._decide(config, result, verdict.decision, verdict.reason_code)
        self._audit(
            config,
            request=request,
            decision=outcome[0],
            reason_code=outcome[1],
            detail=outcome[2],
            categories=result.categories,
            duration_ms=_elapsed_ms(started),
            sha256=result.sha256,
            escalated=result.escalated,
        )
        return outcome[3]

    def _decide(
        self,
        config: RouterConfig,
        result: BodyResult,
        guard_decision: str,
        guard_code: str,
    ) -> tuple[str, str, str, Any]:
        """Turn a guard verdict into the gRPC result and the audit fields."""
        decision = str(guard_decision)

        if decision == Decision.DENY:
            return (
                Decision.DENY,
                guard_code,
                _DENY_DETAILS.get(guard_code, "request denied by the privacy router"),
                pb.HttpRequestResult(
                    decision=pb.DECISION_DENY,
                    reason_code=_safe_reason_code(guard_code),
                    reason="request denied by the InkWell privacy router",
                ),
            )

        if decision == Decision.REDACT and config.mode == "deny":
            return (
                Decision.DENY,
                guard_code,
                "redaction is disabled; the request is refused instead",
                pb.HttpRequestResult(
                    decision=pb.DECISION_DENY,
                    reason_code=_safe_reason_code(guard_code),
                    reason="request denied by the InkWell privacy router",
                ),
            )

        if decision == Decision.REDACT:
            if result.binary:
                return (
                    Decision.DENY,
                    REASON_BINARY_BODY,
                    "a binary body cannot be inspected",
                    pb.HttpRequestResult(
                        decision=pb.DECISION_DENY,
                        reason_code=REASON_BINARY_BODY,
                        reason="request denied by the InkWell privacy router",
                    ),
                )
            if not result.changed:
                return (
                    Decision.DENY,
                    REASON_REDACTION_NOT_APPLIED,
                    "personal detail was detected but no redaction was applied",
                    pb.HttpRequestResult(
                        decision=pb.DECISION_DENY,
                        reason_code=REASON_REDACTION_NOT_APPLIED,
                        reason="request denied by the InkWell privacy router",
                    ),
                )
            return (
                Decision.REDACT,
                guard_code,
                "personal detail redacted from the request body",
                pb.HttpRequestResult(
                    decision=pb.DECISION_ALLOW,
                    body=bytes(result.body),
                    has_body=True,
                    reason="request body redacted by the InkWell privacy router",
                    reason_code=_safe_reason_code(guard_code),
                ),
            )

        return (
            Decision.ALLOW,
            guard_code,
            "no personal detail in the request",
            pb.HttpRequestResult(
                decision=pb.DECISION_ALLOW,
                reason_code=_safe_reason_code(guard_code),
            ),
        )

    # -- helpers -----------------------------------------------------------

    def _config_for(self, request) -> RouterConfig | None:
        try:
            return RouterConfig(**_struct_to_dict(request.config))
        except Exception as exc:
            LOG.warning("invalid policy config (%s)", type(exc).__name__)
            return None

    def _escalation_transport(self, config: RouterConfig):
        if not config.escalate:
            return None
        # The gateway kills the RPC at its registration timeout. Give the
        # optional Nano call a budget that fits inside it, rather than the
        # transport's 60-second default: a timeout there becomes a
        # fail-closed denial, not a slow allow.
        return http_transport(timeout=max(0.05, config.timeout_ms / 1000.0))

    def _audit(
        self,
        config: RouterConfig,
        *,
        request,
        decision: str,
        reason_code: str,
        detail: str,
        categories: list[str],
        duration_ms: float,
        sha256: str,
        escalated: bool = False,
    ) -> None:
        path = config.audit_log_path
        if not path:
            return
        if self._audit_log is None or self._audit_path != path:
            self._audit_log = AuditLog(path)
            self._audit_path = path
        event = AuditEvent(
            timestamp=now_iso(),
            event_type=_AUDIT_EVENT_TYPES.get(str(decision), f"router.{decision}"),
            decision=str(decision),
            reason_code=_safe_reason_code(reason_code),
            host=(request.target.host or "").strip().lower(),
            detail=detail,
            raw={
                "path": _safe_path(request.target.path),
                "request_id": request.context.request_id,
                "sandbox": request.context.sandbox,
                "sandbox_id": request.context.sandbox_id,
                "categories": categories,
                "duration_ms": round(duration_ms, 3),
                "body_sha256": sha256,
                "escalated": escalated,
            },
        )
        try:
            with self._audit_lock:
                self._audit_log.append(event)
        except OSError as exc:
            # An audit trail that cannot be written is not an audit trail.
            # Fail the request closed rather than let it through unrecorded.
            LOG.error("audit log unavailable (%s)", type(exc).__name__)
            raise

    def EvaluateWebSocketSession(self, request_iterator, context):
        # Not advertised in the manifest, so the gateway should never call
        # it. Return an empty stream rather than raising: a gRPC status is a
        # clearer diagnostic than a Python traceback in the gateway log.
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("ledger-router does not inspect WebSocket messages")
        return iter(())


_DENY_DETAILS = {
    "personal_detail_to_unexpected_destination": "personal detail aimed at an unexpected host",
    "destination_not_associated_with_task": "destination not associated with the task",
    "third_party_model_echo_forbidden": "third-party model echo requested",
    "bulk_personal_detail_to_third_party": "bulk personal detail in a third-party request",
    "image_to_unapproved_model": "document image aimed at a model that is not the OCR model",
    "destination_forbidden_for_personal_detail": "destination forbidden for personal detail",
}


def _decoded_body(raw: bytes) -> str:
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


def serve(
    *,
    bind: str = "0.0.0.0",
    port: int = 50051,
    max_workers: int = 8,
    max_payload_bytes: int = 4 * 1024 * 1024,
    default_config: RouterConfig | None = None,
) -> grpc.Server:
    """Start the router. Returns the server so a test can stop it."""
    server = grpc.server(
        ThreadPoolExecutor(max_workers=max_workers),
        maximum_concurrent_rpcs=max_workers,
    )
    servicer = LedgerRouterServicer(
        default_config=default_config,
        max_payload_bytes=max_payload_bytes,
    )
    pb_grpc.add_SupervisorMiddlewareServicer_to_server(servicer, server)

    try:
        from grpc_health.v1 import health, health_pb2, health_pb2_grpc

        health_servicer = health.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
        serving = health_pb2.HealthCheckResponse.SERVING
        health_servicer.set("", serving)
        health_servicer.set("openshell.middleware.v1.SupervisorMiddleware", serving)
    except ImportError:  # pragma: no cover - optional dependency
        LOG.info("grpc health checking is not installed")

    address = f"{bind}:{port}"
    bound = server.add_insecure_port(address)
    if bound == 0:
        raise RuntimeError(f"could not bind the middleware service to {address}")
    LOG.info(
        "ledger-router %s listening on %s (max payload %d bytes)",
        IMPLEMENTATION_VERSION,
        address,
        max_payload_bytes,
    )
    return server


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LEDGER_ROUTER_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
