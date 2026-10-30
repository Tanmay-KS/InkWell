"""Tests for the OpenShell supervisor middleware.

These run the real gRPC service over a loopback port, not just the pure
functions, because the failure this suite exists to catch is a contract
mismatch: a manifest the gateway rejects, a body the router mangles, or a
redaction that is decided but never applied.

The audit assertions are as important as the redaction ones. An audit log
that records the value it was supposed to protect is a second leak.
"""

from __future__ import annotations

import json
import socket

import pytest

pytest.importorskip("grpc")

import grpc
from generated import supervisor_middleware_pb2 as pb
from generated import supervisor_middleware_pb2_grpc as pb_grpc
from google.protobuf import json_format, struct_pb2
from ledger_router.service import serve

TOKEN_FACTORY = "api.tokenfactory.nebius.com"
TAVILY = "api.tavily.com"
OCR_MODEL = "nvidia/Nemotron-3.5-Super-VL"
ULTRA_MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def config_dict(audit_path, **overrides):
    config = {
        "mode": "redact",
        "redact_models": [OCR_MODEL, ULTRA_MODEL],
        "redact_hosts": [TAVILY],
        "allowed_hosts": [TOKEN_FACTORY, TAVILY],
        "third_party_hosts": [TAVILY],
        "forbidden_flags": ["include_answer", "include_raw_content"],
        "deny_threshold": 3,
        "ocr_models": [OCR_MODEL],
        "audit_log_path": str(audit_path),
    }
    config.update(overrides)
    return config


def as_struct(config: dict) -> struct_pb2.Struct:
    return json_format.ParseDict(config, struct_pb2.Struct())


@pytest.fixture()
def router(tmp_path):
    port = free_port()
    server = serve(bind="127.0.0.1", port=port)
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    grpc.channel_ready_future(channel).result(timeout=10)
    stub = pb_grpc.SupervisorMiddlewareStub(channel)
    yield stub, tmp_path / "audit.jsonl", tmp_path
    channel.close()
    server.stop(0).wait()


def evaluate(stub, *, host, path, body, config, body_bytes=None):
    request = pb.HttpRequestEvaluation(
        phase=pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
        context=pb.RequestContext(request_id="req-1", sandbox="inkwell-hermes"),
        config=as_struct(config),
        target=pb.HttpRequestTarget(host=host, port=443, method="POST", path=path),
        body=body_bytes if body_bytes is not None else json.dumps(body).encode(),
        middleware_name="ledger-router",
    )
    return stub.EvaluateHttpRequest(request)


def test_describe_declares_the_pre_credentials_http_binding(router):
    stub, _, _ = router
    manifest = stub.Describe(pb.MiddlewareDescribeRequest())
    assert manifest.name == "ledger-router"
    assert len(manifest.bindings) == 1
    binding = manifest.bindings[0]
    assert binding.operation == pb.SUPERVISOR_MIDDLEWARE_OPERATION_HTTP_REQUEST
    assert binding.phase == pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS
    assert binding.max_payload_bytes >= 1024
    assert manifest.extension.protocol_version.major == 1
    assert "openshell.supervisor-middleware.contract" in manifest.extension.supported_capabilities


def test_validate_config_accepts_a_valid_policy(router):
    stub, audit, _ = router
    response = stub.ValidateConfig(
        pb.ValidateConfigRequest(
            config=as_struct(config_dict(audit)), middleware_name="ledger-router"
        )
    )
    assert response.valid
    assert response.reason == ""


def test_validate_config_rejects_an_unknown_field_without_echoing_it(router):
    stub, audit, _ = router
    response = stub.ValidateConfig(
        pb.ValidateConfigRequest(
            config=as_struct(config_dict(audit, deny_thresholdh=3)),
            middleware_name="ledger-router",
        )
    )
    assert not response.valid
    assert "deny_thresholdh" in response.reason


def test_token_factory_body_is_redacted_and_the_model_is_preserved(router):
    stub, audit, _ = router
    result = evaluate(
        stub,
        host=TOKEN_FACTORY,
        path="/v1/chat/completions",
        body={
            "model": OCR_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": "Policy holder: Devin Sullivan account 0847-4851",
                }
            ],
        },
        config=config_dict(audit),
    )
    assert result.decision == pb.DECISION_ALLOW
    assert result.has_body
    forwarded = json.loads(result.body)
    assert forwarded["model"] == OCR_MODEL
    content = forwarded["messages"][0]["content"]
    assert "Devin Sullivan" not in content
    assert "0847-4851" not in content
    assert "[NAME_1]" in content
    assert "[ACCOUNT_1]" in content
    assert result.reason_code == "personal_detail_redacted"

    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "router.redacted"
    assert event["decision"] == "redact"
    assert event["raw"]["categories"] == ["account", "name"]
    assert "Devin Sullivan" not in json.dumps(event)
    assert "0847-4851" not in json.dumps(event)


def test_a_clean_request_is_allowed_without_a_body_replacement(router):
    stub, audit, _ = router
    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "cancellation policy"},
        config=config_dict(audit),
    )
    assert result.decision == pb.DECISION_ALLOW
    assert not result.has_body
    assert result.reason_code == "no_personal_detail"
    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "router.allowed"
    assert event["decision"] == "allow"


def test_a_third_party_echo_is_denied(router):
    stub, audit, _ = router
    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "cancel 0847-4851", "include_answer": True},
        config=config_dict(audit),
    )
    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "third_party_model_echo_forbidden"
    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "router.denied"
    assert event["decision"] == "deny"
    assert "0847-4851" not in json.dumps(event)


def test_a_bulk_record_to_a_third_party_is_denied(router):
    stub, audit, _ = router
    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={
            "query": (
                "Devin Sullivan 0847-4851 1003 Braddock Boulevard, Marrow, CO 80211 "
                "devin@example.com"
            )
        },
        config=config_dict(audit),
    )
    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "bulk_personal_detail_to_third_party"


def test_a_document_image_to_the_wrong_model_is_denied(router):
    stub, audit, _ = router
    result = evaluate(
        stub,
        host=TOKEN_FACTORY,
        path="/v1/chat/completions",
        body={
            "model": ULTRA_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ],
        },
        config=config_dict(audit),
    )
    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "image_to_unapproved_model"


def test_a_query_string_in_the_path_is_not_stored_in_the_audit(router):
    stub, audit, _ = router
    result = evaluate(
        stub,
        host=TAVILY,
        path="/search?query=Devin+Sullivan+0847-4851",
        body={"query": "cancel 0847-4851"},
        config=config_dict(audit),
    )
    assert result.decision == pb.DECISION_ALLOW
    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert event["raw"]["path"] == "/search"
    assert "0847-4851" not in json.dumps(event)
    assert "Devin" not in json.dumps(event)


def test_an_unwritable_audit_log_denies_even_a_clean_request(router):
    stub, _, tmp = router
    # A path whose parent is a regular file cannot be created as a log.
    blocker = tmp / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "cancellation policy"},
        config=config_dict(blocker / "audit.jsonl"),
    )
    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "audit_unavailable"


def test_a_binary_body_is_denied_on_an_inspected_host(router):
    stub, audit, _ = router
    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body=None,
        body_bytes=b"\xff\xfe\x00\x01",
        config=config_dict(audit),
    )
    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "binary_body_not_inspectable"


def test_deny_mode_refuses_instead_of_redacting(router):
    stub, audit, _ = router
    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "cancel 0847-4851"},
        config=config_dict(audit, mode="deny"),
    )
    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "personal_detail_redacted"


def test_every_decision_is_audited(router):
    stub, audit, _ = router
    evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "cancellation policy"},
        config=config_dict(audit),
    )
    evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "cancel 0847-4851"},
        config=config_dict(audit),
    )
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [event["decision"] for event in events] == ["allow", "redact"]
    assert all(event["event_type"].startswith("router.") for event in events)


def test_escalation_transport_failure_denies_and_does_not_forward(router, monkeypatch):
    stub, audit, _ = router

    def factory(*args, **kwargs):
        def send(request):
            raise RuntimeError("model unavailable")

        return send

    monkeypatch.setattr("ledger_router.service.http_transport", factory)

    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "Email sam@example.com about Jordan's policy."},
        config=config_dict(audit, escalate=True),
    )

    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "escalation_failed"
    assert not result.has_body
    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "router.denied"
    assert event["decision"] == "deny"
    assert "sam@example.com" not in json.dumps(event)
    assert "Jordan" not in json.dumps(event)


def test_malformed_escalation_answer_denies_instead_of_forwarding(router, monkeypatch):
    stub, audit, _ = router

    def factory(*args, **kwargs):
        return lambda request: "not JSON"

    monkeypatch.setattr("ledger_router.service.http_transport", factory)

    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "Email sam@example.com about Jordan's policy."},
        config=config_dict(audit, escalate=True),
    )

    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "escalation_failed"
    assert not result.has_body


def test_nonmatching_escalation_span_denies_instead_of_forwarding(router, monkeypatch):
    stub, audit, _ = router

    def factory(*args, **kwargs):
        return lambda request: '{"spans": ["not in the query"]}'

    monkeypatch.setattr("ledger_router.service.http_transport", factory)

    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "Email sam@example.com about Jordan's policy."},
        config=config_dict(audit, escalate=True),
    )

    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "escalation_failed"
    assert not result.has_body


def test_valid_empty_escalation_answer_forwards_the_redacted_body(router, monkeypatch):
    stub, audit, _ = router

    def factory(*args, **kwargs):
        return lambda request: '{"spans": []}'

    monkeypatch.setattr("ledger_router.service.http_transport", factory)

    result = evaluate(
        stub,
        host=TAVILY,
        path="/search",
        body={"query": "Email sam@example.com about the policy."},
        config=config_dict(audit, escalate=True),
    )

    assert result.decision == pb.DECISION_ALLOW
    assert result.has_body
    forwarded = json.loads(result.body)
    assert "sam@example.com" not in forwarded["query"]
    assert "[EMAIL_1]" in forwarded["query"]
