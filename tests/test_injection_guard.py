"""Tests for the Phase 2 exfiltration guard.

The guard is the content-aware half of the privacy router: the network
policy decides *where* traffic may go, and this decides *what* may be in
it. The cases below are the three decisions plus the two rules that make
the injection demo possible.
"""

from __future__ import annotations

from privacy_router.injection_guard import (
    REASON_BULK_EXFIL,
    REASON_CLEAN,
    REASON_DENY_HOST,
    REASON_EXFIL_ATTEMPT,
    REASON_IMAGE_WRONG_MODEL,
    REASON_REDACTED,
    REASON_THIRD_PARTY_ECHO,
    REASON_UNKNOWN_DESTINATION,
    Decision,
    GuardPolicy,
    evaluate_request,
)

TAVILY = "api.tavily.com"
TOKEN_FACTORY = "api.tokenfactory.nebius.com"


def policy(**overrides) -> GuardPolicy:
    base = {
        "allowed_hosts": (TOKEN_FACTORY, TAVILY),
        "third_party_hosts": (TAVILY,),
        "forbidden_flags": ("include_answer", "include_raw_content"),
        "deny_threshold": 3,
    }
    base.update(overrides)
    return GuardPolicy(**base)


def test_a_clean_request_is_allowed():
    verdict = evaluate_request(
        host=TAVILY, path="/search", body="cancellation policy", policy=policy()
    )
    assert verdict.decision == Decision.ALLOW
    assert verdict.reason_code == REASON_CLEAN
    assert verdict.findings == []


def test_a_single_identifier_is_redacted_not_denied():
    verdict = evaluate_request(
        host=TAVILY,
        path="/search",
        body="cancel policy 0847-4851",
        policy=policy(),
    )
    assert verdict.decision == Decision.REDACT
    assert verdict.reason_code == REASON_REDACTED
    assert "account" in verdict.findings


def test_a_bulk_record_to_a_third_party_is_denied():
    body = (
        "Devin Sullivan 0847-4851 1003 Braddock Boulevard, Marrow, CO 80211 "
        "devin@example.com"
    )
    verdict = evaluate_request(host=TAVILY, path="/search", body=body, policy=policy())
    assert verdict.decision == Decision.DENY
    assert verdict.reason_code == REASON_BULK_EXFIL
    assert len(verdict.findings) >= 3


def test_a_third_party_model_echo_is_denied_even_for_one_identifier():
    verdict = evaluate_request(
        host=TAVILY,
        path="/search",
        body="cancel policy 0847-4851",
        flags=["include_answer"],
        policy=policy(),
    )
    assert verdict.decision == Decision.DENY
    assert verdict.reason_code == REASON_THIRD_PARTY_ECHO


def test_a_destination_outside_the_allowlist_is_denied():
    verdict = evaluate_request(
        host="collect.evil.example",
        path="/upload",
        body="Devin Sullivan",
        policy=policy(),
    )
    assert verdict.decision == Decision.DENY
    assert verdict.reason_code == REASON_EXFIL_ATTEMPT


def test_an_explicitly_forbidden_host_is_denied():
    verdict = evaluate_request(
        host=TAVILY,
        path="/search",
        body="cancel policy 0847-4851",
        policy=policy(deny_hosts=(TAVILY,)),
    )
    assert verdict.decision == Decision.DENY
    assert verdict.reason_code == REASON_DENY_HOST


def test_a_host_outside_the_task_is_denied():
    verdict = evaluate_request(
        host=TAVILY,
        path="/search",
        body="cancel policy 0847-4851",
        policy=policy(task_hosts=(TOKEN_FACTORY,)),
    )
    assert verdict.decision == Decision.DENY
    assert verdict.reason_code == REASON_UNKNOWN_DESTINATION


def test_an_image_is_only_allowed_to_the_approved_ocr_model():
    verdict = evaluate_request(
        host=TOKEN_FACTORY,
        path="/v1/chat/completions",
        body="",
        model="nvidia/Nemotron-3-Ultra-550b-a55b",
        has_image=True,
        policy=policy(ocr_models=("nvidia/Nemotron-3.5-Super-VL",)),
    )
    assert verdict.decision == Decision.DENY
    assert verdict.reason_code == REASON_IMAGE_WRONG_MODEL

    allowed = evaluate_request(
        host=TOKEN_FACTORY,
        path="/v1/chat/completions",
        body="",
        model="nvidia/Nemotron-3.5-Super-VL",
        has_image=True,
        policy=policy(ocr_models=("nvidia/Nemotron-3.5-Super-VL",)),
    )
    assert allowed.decision == Decision.ALLOW


def test_a_household_name_in_prose_is_found_when_the_names_are_known():
    body = "Please cancel the plan for Devin Sullivan."
    with_names = evaluate_request(
        host=TAVILY,
        path="/search",
        body=body,
        names=["Devin Sullivan"],
        policy=policy(),
    )
    assert with_names.decision == Decision.REDACT
    assert "name" in with_names.findings


def test_the_verdict_never_contains_the_value():
    value = "0847-4851"
    verdict = evaluate_request(
        host=TAVILY, path="/search", body=f"cancel policy {value}", policy=policy()
    )
    assert value not in verdict.detail
    assert all(value not in finding for finding in verdict.findings)
