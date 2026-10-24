"""Regression tests for model-assisted redaction.

These tests are deliberately offline: every transport is a deterministic
fake, and no API key or network access is needed. The important cases are
the failures. A model that returns malformed JSON, an empty answer, an
unexpected shape, or a span that does not match the text must not be
treated as a successful privacy check.
"""

from __future__ import annotations

import json

import pytest

from privacy_router.egress import OutboundRequest, Transport
from privacy_router.redact import (
    Category,
    RedactionError,
    parse_residual_response,
    parse_residual_spans,
    redact,
)

PII_TEXT = "Email sam@example.com about Jordan's policy."


def residual_answer(*spans: str) -> str:
    """Build a valid model answer without hand-escaping quotes."""
    return json.dumps({"spans": list(spans)})


def fake_transport(
    response: object,
    calls: list[OutboundRequest] | None = None,
) -> Transport:
    """Return a deterministic transport. It never touches the network."""

    def send(request: OutboundRequest) -> str:
        if calls is not None:
            calls.append(request)
        return response  # type: ignore[return-value]

    return send


def raising_transport(exc: BaseException) -> Transport:
    def send(request: OutboundRequest) -> str:
        raise exc

    return send


def test_local_redaction_works_without_a_model_call():
    result = redact(PII_TEXT)

    assert "sam@example.com" not in result.text
    assert "[EMAIL_1]" in result.text
    assert result.escalated is False
    assert result.residual_checked is False


def test_model_call_carries_only_locally_redacted_text():
    calls: list[OutboundRequest] = []

    result = redact(
        PII_TEXT,
        transport=fake_transport(residual_answer("Jordan's"), calls),
    )

    assert result.residual_checked is True
    [request] = calls
    assert request.kind == "redact_call"
    # The email was already masked locally. The residual model sees the
    # placeholder, not the address.
    assert "sam@example.com" not in request.payload
    assert "[EMAIL_1]" in request.payload
    # The prose name is the residual detail this pass exists to catch.
    assert "Jordan's" in request.payload


def test_valid_residual_span_is_applied():
    result = redact(
        "Please cancel the policy under Jordan's name.",
        transport=fake_transport(residual_answer("Jordan's")),
    )

    assert "Jordan's" not in result.text
    assert "[OTHER_1]" in result.text
    assert result.escalated is True
    assert result.residual_checked is True
    assert result.redactions[-1].category is Category.OTHER


def test_empty_span_list_is_a_successful_no_residual_answer():
    result = redact(
        PII_TEXT,
        transport=fake_transport(residual_answer()),
    )

    assert result.escalated is True
    assert result.residual_checked is True
    assert "sam@example.com" not in result.text
    assert "[EMAIL_1]" in result.text


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ("", "empty_response"),
        ("not JSON", "malformed_response"),
        ('{"spans": ', "malformed_response"),
        (json.dumps({"unexpected": []}), "unexpected_response"),
        (json.dumps({"spans": "Jordan"}), "unexpected_response"),
        (json.dumps({"spans": [123]}), "unexpected_response"),
        (residual_answer("[NAME_1]"), "no_usable_spans"),
        (residual_answer("x" * 201), "unusable_span"),
        (residual_answer("an unrelated phrase"), "unapplied_spans"),
    ],
)
def test_unusable_model_responses_fail_closed(response: str, reason: str):
    with pytest.raises(RedactionError) as caught:
        redact(PII_TEXT, transport=fake_transport(response))

    error = caught.value
    assert error.reason == reason
    assert error.result is not None
    assert "sam@example.com" not in error.result.text
    assert "[EMAIL_1]" in error.result.text
    assert error.result.residual_checked is False


def test_partial_span_application_preserves_redactions_but_still_fails():
    with pytest.raises(RedactionError) as caught:
        redact(
            PII_TEXT,
            transport=fake_transport(
                residual_answer("Jordan's", "an unrelated phrase")
            ),
        )

    error = caught.value
    assert error.reason == "unapplied_spans"
    assert error.result is not None
    assert "sam@example.com" not in error.result.text
    assert "Jordan's" not in error.result.text
    assert "[EMAIL_1]" in error.result.text
    assert "[OTHER_1]" in error.result.text
    assert error.result.residual_checked is False


def test_transport_exception_fails_closed_without_echoing_text():
    exc = RuntimeError("network down for sam@example.com and Jordan")
    with pytest.raises(RedactionError) as caught:
        redact(PII_TEXT, transport=raising_transport(exc))

    error = caught.value
    assert error.reason == "transport_error"
    assert error.cause_type == "RuntimeError"
    assert error.result is not None
    assert "sam@example.com" not in error.result.text
    assert "[EMAIL_1]" in error.result.text
    # The exception must not become a second copy of the sensitive input.
    assert "sam@example.com" not in str(error)
    assert "Jordan" not in str(error)


def test_a_span_covered_by_a_longer_applied_span_is_not_a_failure():
    result = redact(
        "Please cancel the policy under Jordan's name.",
        transport=fake_transport(residual_answer("Jordan's", "Jordan")),
    )

    assert "Jordan" not in result.text
    assert result.text.count("[OTHER_1]") == 1
    assert result.residual_checked is True


def test_a_model_span_cannot_rewrite_inside_an_existing_placeholder():
    with pytest.raises(RedactionError) as caught:
        redact(
            PII_TEXT,
            transport=fake_transport(residual_answer("EMAIL_1")),
        )

    error = caught.value
    assert error.reason == "unapplied_spans"
    assert error.result is not None
    assert "[EMAIL_1]" in error.result.text
    assert "[[OTHER_" not in error.result.text


def test_parser_distinguishes_empty_success_from_failure():
    empty = parse_residual_response(residual_answer())
    assert empty.ok is True
    assert empty.spans == []

    assert parse_residual_response("").error == "empty_response"
    assert parse_residual_response("not JSON").error == "malformed_response"
    assert (
        parse_residual_response(json.dumps({"unexpected": []})).error
        == "unexpected_response"
    )


def test_parser_accepts_a_valid_span_with_an_apostrophe():
    parsed = parse_residual_response(residual_answer("Jordan's"))
    assert parsed.ok is True
    assert parsed.spans == ["Jordan's"]


def test_lenient_wrapper_remains_compatible_but_is_not_a_privacy_decision():
    assert parse_residual_spans("not JSON") == []
    assert parse_residual_spans(residual_answer("Jordan's")) == ["Jordan's"]


def test_process_body_raises_instead_of_returning_a_partial_body():
    from ledger_router.body import process_body
    from ledger_router.config import RouterConfig

    config = RouterConfig(escalate=True)
    raw = json.dumps({"query": PII_TEXT}).encode("utf-8")

    with pytest.raises(RedactionError):
        process_body(
            host="api.tavily.com",
            path="/search",
            raw=raw,
            config=config,
            transport=raising_transport(RuntimeError("model unavailable")),
        )
