"""Tests for reading OpenShell's OCSF event stream.

The shapes here are taken from the classes OpenShell actually emits: HTTP
Activity 4002 for a middleware invocation, Network Activity 4001 for a
policy denial, and Detection Finding 2004 for a finding. The importer has
to be defensive because it reads a log export, not a value the process
controls.
"""

from __future__ import annotations

import json

from privacy_router.audit_log import from_ocsf


def http_event(**overrides):
    record = {
        "class_uid": 4002,
        "class_name": "HTTP Activity",
        "category_uid": 4,
        "activity_id": 99,
        "activity_name": "Other",
        "time": 1_760_000_000_000,
        "action_id": 1,
        "action": "Allowed",
        "disposition_id": 1,
        "disposition": "Allowed",
        "severity_id": 1,
        "http_request": {
            "http_method": "POST",
            "url": {
                "scheme": "https",
                "hostname": "api.tokenfactory.nebius.com",
                "path": "/v1/chat/completions",
                "port": 443,
            },
        },
        "dst_endpoint": {"domain": "api.tokenfactory.nebius.com", "port": 443},
        "firewall_rule": {"name": "inkwell-hermes", "type": "middleware"},
        "unmapped": {"transformed": False, "failed": False},
        "message": "MIDDLEWARE ledger-router inkwell/privacy-router decision=Allow",
    }
    record.update(overrides)
    return record


def test_a_transformed_middleware_call_is_a_router_redaction():
    [event] = from_ocsf([http_event(unmapped={"transformed": True, "failed": False})])
    assert event.event_type == "router.redacted"
    assert event.decision == "redact"
    assert event.reason_code == "personal_detail_redacted"
    assert event.host == "api.tokenfactory.nebius.com"
    assert event.timestamp.startswith("2025-10-09T")
    assert event.timestamp.endswith("+00:00")


def test_an_untouched_middleware_call_is_a_router_allow():
    [event] = from_ocsf([http_event()])
    assert event.event_type == "router.allowed"
    assert event.decision == "allow"
    assert event.reason_code == "no_personal_detail"


def test_a_denial_reason_code_is_read_from_the_stable_status_detail():
    [event] = from_ocsf(
        [
            http_event(
                action_id=2,
                action="Denied",
                disposition_id=2,
                disposition="Blocked",
                status_id=2,
                status="Failure",
                status_detail="middleware_denied:ledger-router:third_party_model_echo_forbidden",
            )
        ]
    )
    assert event.event_type == "router.denied"
    assert event.decision == "deny"
    assert event.reason_code == "third_party_model_echo_forbidden"
    assert "third_party_model_echo_forbidden" in event.detail


def test_a_fail_open_is_not_silently_an_allow():
    [event] = from_ocsf(
        [http_event(unmapped={"transformed": False, "failed": True})]
    )
    assert event.event_type == "router.failed_open"
    assert event.decision == "allow"
    assert event.reason_code == "middleware_failed"


def test_a_detection_finding_becomes_a_router_finding():
    [event] = from_ocsf(
        [
            {
                "class_uid": 2004,
                "class_name": "Detection Finding",
                "activity_id": 1,
                "time": 1_760_000_000_000,
                "severity_id": 4,
                "status_id": 2,
                "finding_info": {
                    "uid": "openshell.middleware.traffic_uninspectable",
                    "title": "Supervisor middleware cannot inspect this traffic",
                },
                "dst_endpoint": {"domain": "collect.evil.example", "port": 443},
                "unmapped": {"middleware": "ledger-router"},
            }
        ]
    )
    assert event.event_type == "router.finding"
    assert event.reason_code == "openshell.middleware.traffic_uninspectable"
    assert event.host == "collect.evil.example"
    assert event.detail == "Supervisor middleware cannot inspect this traffic"


def test_a_network_policy_denial_is_kept_separate_from_the_router():
    [event] = from_ocsf(
        [
            {
                "class_uid": 4001,
                "class_name": "Network Activity",
                "activity_id": 2,
                "time": 1_760_000_000_000,
                "action_id": 2,
                "action": "Denied",
                "disposition_id": 2,
                "disposition": "Blocked",
                "status_detail": "network_policy_denied",
                "dst_endpoint": {"domain": "collect.evil.example", "port": 443},
            }
        ]
    )
    assert event.event_type == "network.denied"
    assert event.decision == "deny"
    assert event.reason_code == "network_policy_denied"
    assert event.host == "collect.evil.example"


def test_an_unknown_class_is_kept_but_not_guessed_at():
    [event] = from_ocsf([{"class_uid": 6003, "time": 1_760_000_000_000}])
    assert event.event_type == "ocsf.event"
    assert event.decision is None
    assert "6003" in event.detail


def test_malformed_records_do_not_stop_the_export():
    events = from_ocsf(
        [
            None,
            "not json",
            {},
            {"class_uid": "not-a-number"},
            http_event(),
        ]
    )
    assert len(events) == 1
    assert events[0].event_type == "router.allowed"


def test_a_string_class_uid_is_accepted():
    [event] = from_ocsf([http_event(class_uid="4002")])
    assert event.event_type == "router.allowed"


def test_the_raw_record_keeps_the_decision_but_not_the_request_or_free_text():
    record = http_event(
        status_detail="account 0847-4851",
        message="cancel 0847-4851",
        http_request={
            "http_method": "GET",
            "url": {
                "scheme": "https",
                "hostname": "api.tavily.com",
                "path": "/search?query=0847-4851",
                "port": 443,
            },
        },
    )
    [event] = from_ocsf([record])
    serialized = json.dumps(event.to_json())
    assert "0847-4851" not in serialized
    assert "http_request" not in event.raw
    assert "status_detail" not in event.raw
    assert "message" not in event.raw
    assert event.raw["class_uid"] == 4002
    assert event.raw["unmapped"] == {"transformed": False, "failed": False}


def test_the_detail_is_redacted_before_it_is_stored():
    [event] = from_ocsf([http_event(status_detail="account 0847-4851")])
    assert "0847-4851" not in (event.detail or "")
    assert "[ACCOUNT_1]" in (event.detail or "")


def test_a_finding_title_is_scrubbed_and_not_duplicated_in_raw():
    [event] = from_ocsf(
        [
            {
                "class_uid": 2004,
                "time": 1_760_000_000_000,
                "action_id": 2,
                "disposition_id": 2,
                "finding_info": {
                    "uid": "openshell.middleware.ledger-router",
                    "title": "account 0847-4851",
                },
            }
        ]
    )
    assert event.event_type == "router.finding"
    assert "0847-4851" not in (event.detail or "")
    assert "0847-4851" not in json.dumps(event.to_json())
    assert "title" not in event.raw.get("finding_info", {})
